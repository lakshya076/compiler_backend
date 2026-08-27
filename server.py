import subprocess
import tempfile
import os
import uuid
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Maps language extension -> Docker image name (defined in docker-compose.yml)
LANGUAGE_CONFIG = {
    "py":   {"image": "compiler-python",     "filename": "main.py",   "cmd": ["python3", "main.py"]},
    "js":   {"image": "compiler-node",       "filename": "main.js",   "cmd": ["node", "main.js"]},
    "java": {"image": "compiler-java",       "filename": "Main.java", "cmd": ["sh", "-c", "javac Main.java && java Main"]},
    "c":    {"image": "compiler-c",          "filename": "main.c",    "cmd": ["sh", "-c", "gcc main.c -o main && ./main"]},
    "cpp":  {"image": "compiler-cpp",        "filename": "main.cpp",  "cmd": ["sh", "-c", "g++ main.cpp -o main && ./main"]},
    "rs":   {"image": "compiler-rust",       "filename": "main.rs",   "cmd": ["sh", "-c", "rustc main.rs -o main && ./main"]},
    "rb":   {"image": "compiler-ruby",       "filename": "main.rb",   "cmd": ["ruby", "main.rb"]},
    "jl":   {"image": "compiler-julia",      "filename": "main.jl",   "cmd": ["julia", "main.jl"]},
}

TIMEOUT_SECONDS = 15


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/compile", methods=["POST"])
def compile_code():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    language = data.get("language", "").strip()
    code = data.get("code", "").strip()

    if not language:
        return jsonify({"error": "Missing 'language' field."}), 400
    if not code:
        return jsonify({"error": "Missing 'code' field."}), 400
    if language not in LANGUAGE_CONFIG:
        return jsonify({"error": f"Unsupported language: '{language}'. Supported: {list(LANGUAGE_CONFIG.keys())}"}), 400

    config = LANGUAGE_CONFIG[language]
    container_name = f"compiler-run-{uuid.uuid4().hex[:12]}"

    # Write source code to a temp directory that will be mounted into the container
    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(tmpdir, config["filename"])
        with open(source_path, "w", encoding="utf-8") as f:
            f.write(code)

        docker_cmd = [
            "docker", "run",
            "--rm",                          # auto-remove after exit
            "--name", container_name,
            "--network", "none",             # no network access from user code
            "--memory", "128m",              # max 128 MB RAM
            "--cpus", "0.5",                 # max 0.5 CPU cores
            "--pids-limit", "64",            # prevent fork bombs
            "-v", f"{tmpdir}:/code:ro",      # mount source as read-only
            "-w", "/code",                   # set working directory
            config["image"],
        ] + config["cmd"]

        logger.info(f"Running container '{container_name}' for language '{language}'")

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )

            stdout = result.stdout
            stderr = result.stderr

            if result.returncode == 0:
                output = stdout if stdout else "(No output)"
                return jsonify({"output": output}), 200
            else:
                # Compilation or runtime error — return stderr as the error message
                error_output = stderr if stderr else stdout if stdout else "Process exited with a non-zero code."
                return jsonify({"error": error_output}), 200  # 200 so Flutter shows it in the output panel

        except subprocess.TimeoutExpired:
            # Kill the container if it's still running
            subprocess.run(["docker", "kill", container_name], capture_output=True)
            logger.warning(f"Container '{container_name}' timed out after {TIMEOUT_SECONDS}s")
            return jsonify({"error": f"Execution timed out after {TIMEOUT_SECONDS} seconds."}), 200

        except FileNotFoundError:
            logger.error("Docker is not installed or not in PATH.")
            return jsonify({"error": "Docker is not available on this server."}), 500

        except Exception as e:
            logger.exception("Unexpected error during code execution.")
            return jsonify({"error": f"Internal server error: {str(e)}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
