import subprocess
import tempfile
import threading
import time
import os
import uuid
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Compiled output goes to /tmp: /code is read-only and the runner rootfs is read-only.
LANGUAGE_CONFIG = {
    "py":   {"image": "compiler-python",     "filename": "main.py",   "cmd": ["python3", "main.py"]},
    "js":   {"image": "compiler-node",       "filename": "main.js",   "cmd": ["node", "main.js"]},
    "java": {"image": "compiler-java",       "filename": "Main.java", "cmd": ["sh", "-c", "javac -d /tmp Main.java && java -cp /tmp Main"]},
    "c":    {"image": "compiler-c",          "filename": "main.c",    "cmd": ["sh", "-c", "gcc main.c -o /tmp/main && /tmp/main"]},
    "cpp":  {"image": "compiler-cpp",        "filename": "main.cpp",  "cmd": ["sh", "-c", "g++ main.cpp -o /tmp/main && /tmp/main"]},
    "rs":   {"image": "compiler-rust",       "filename": "main.rs",   "cmd": ["sh", "-c", "rustc main.rs -o /tmp/main && /tmp/main"]},
    "rb":   {"image": "compiler-ruby",       "filename": "main.rb",   "cmd": ["ruby", "main.rb"]},
    "jl":   {"image": "compiler-julia",      "filename": "main.jl",   "cmd": ["julia", "main.jl"]},
}

TIMEOUT_SECONDS = 15
MAX_OUTPUT_BYTES = 64 * 1024        # per stream (stdout / stderr)
MAX_CONCURRENT_RUNS = 4             # containers running at once; gunicorn runs 1 worker, so this is global
QUEUE_WAIT_SECONDS = 10             # how long a request waits for a free slot before getting 503
RUNNER_USER = "65534:65534"         # nobody:nogroup

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # max request body

_run_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)


def run_capped(cmd, timeout, max_bytes, kill):
    """Run cmd, keeping at most max_bytes of stdout and of stderr.

    Calls kill() when the timeout passes or output exceeds max_bytes.
    Returns (returncode, stdout, stderr, timed_out, truncated).
    """
    overflow = threading.Event()
    bufs = (bytearray(), bytearray())

    def drain(stream, buf):
        for chunk in iter(lambda: stream.read1(8192), b""):
            room = max_bytes - len(buf)
            if room > 0:
                buf += chunk[:room]
            if len(chunk) > room:
                overflow.set()

    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        readers = [threading.Thread(target=drain, args=(s, b), daemon=True)
                   for s, b in zip((proc.stdout, proc.stderr), bufs)]
        for t in readers:
            t.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        while proc.poll() is None:
            if overflow.wait(0.05):
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
        if proc.poll() is None:
            kill()
            proc.kill()
        proc.wait()
        for t in readers:
            t.join(timeout=5)

    return (proc.returncode,
            bufs[0].decode("utf-8", "replace"),
            bufs[1].decode("utf-8", "replace"),
            timed_out,
            overflow.is_set())


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": f"Code is too large (max {app.config['MAX_CONTENT_LENGTH'] // 1024} KB)."}), 413


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/compile", methods=["POST"])
def compile_code():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body."}), 400

    language = data.get("language", "")
    code = data.get("code", "")
    if not isinstance(language, str) or not isinstance(code, str):
        return jsonify({"error": "'language' and 'code' must be strings."}), 400
    language = language.strip()
    code = code.strip()

    if not language:
        return jsonify({"error": "Missing 'language' field."}), 400
    if not code:
        return jsonify({"error": "Missing 'code' field."}), 400
    if language not in LANGUAGE_CONFIG:
        return jsonify({"error": f"Unsupported language: '{language}'. Supported: {list(LANGUAGE_CONFIG.keys())}"}), 400

    if not _run_slots.acquire(timeout=QUEUE_WAIT_SECONDS):
        return jsonify({"error": "Server is busy. Please try again in a moment."}), 503
    try:
        return _run(language, code)
    finally:
        _run_slots.release()


def _run(language, code):
    config = LANGUAGE_CONFIG[language]
    container_name = f"compiler-run-{uuid.uuid4().hex[:12]}"

    # Write source code to a temp directory that will be mounted into the container
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(tmpdir, config["filename"])
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(code)
        # The runner is an unprivileged user, so it needs read access to the source.
        os.chmod(tmpdir, 0o755)
        os.chmod(source_path, 0o644)

        docker_cmd = [
            "docker", "run",
            "--rm",                          # auto-remove after exit
            "--name", container_name,
            "--network", "none",             # no network access from user code
            "--memory", "128m",              # max 128 MB RAM
            "--cpus", "0.5",                 # max 0.5 CPU cores
            "--pids-limit", "64",            # prevent fork bombs
            "--user", RUNNER_USER,           # never run user code as root
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",                   # user code cannot fill the host disk
            "--tmpfs", "/tmp:rw,exec,nosuid,size=64m",  # only writable place, for compiler output
            "-e", "TMPDIR=/tmp",
            "-e", "HOME=/tmp",
            "-v", f"{tmpdir}:/code:ro",      # mount source as read-only
            "-w", "/code",                   # set working directory
            config["image"],
        ] + config["cmd"]

        logger.info(f"Running container '{container_name}' for language '{language}'")

        def kill():
            subprocess.run(["docker", "kill", container_name], capture_output=True)

        try:
            returncode, stdout, stderr, timed_out, truncated = run_capped(
                docker_cmd, TIMEOUT_SECONDS, MAX_OUTPUT_BYTES, kill)
        except FileNotFoundError:
            logger.error("Docker is not installed or not in PATH.")
            return jsonify({"error": "Docker is not available on this server."}), 500
        except Exception:
            logger.exception("Unexpected error during code execution.")
            return jsonify({"error": "Internal server error."}), 500

        if timed_out:
            logger.warning(f"Container '{container_name}' timed out after {TIMEOUT_SECONDS}s")
            return jsonify({"error": f"Execution timed out after {TIMEOUT_SECONDS} seconds."}), 200

        if truncated:
            logger.warning(f"Container '{container_name}' killed: output over {MAX_OUTPUT_BYTES} bytes")
            text = stdout or stderr
            return jsonify({"output": f"{text}\n[Output truncated at {MAX_OUTPUT_BYTES // 1024} KB]"}), 200

        if returncode == 0:
            output = stdout if stdout else "(No output)"
            return jsonify({"output": output}), 200
        else:
            # Compilation or runtime error — return stderr as the error message
            error_output = stderr if stderr else stdout if stdout else "Process exited with a non-zero code."
            return jsonify({"error": error_output}), 200  # 200 so Flutter shows it in the output panel


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
