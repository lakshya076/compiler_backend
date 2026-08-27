# Code Compiler — Backend

A lightweight Python/Flask API for [code_compiler](https://github.com/lakshya076/code_compiler) that executes code snippets by spinning up
disposable Docker containers — one per request, destroyed immediately after.

## Architecture

```
Flutter App  ──POST /compile──►  Flask API (server.py)
                                      │
                          docker run  │  (via Docker socket)
                                      ▼
                              compiler-python / compiler-node / ...
                                      │
                              (runs code, returns stdout/stderr)
                                      │
                              container auto-removed (--rm)
```

## Prerequisites

- Docker Engine installed and running on the host
- Python 3.12+ (only needed if running the server without Docker)

## Quick Start

### 1. Build all images

```bash
cd backend

# Build the API server image
docker compose build api

# Build all language runner images
docker compose --profile build-only build
```

### 2. Start the API

```bash
docker compose up -d api
```

The server is now listening on **http://localhost:5000**.

### 3. Test it

```bash
curl -X POST http://localhost:5000/compile \
  -H "Content-Type: application/json" \
  -d '{"language": "py", "code": "print(\"Hello from Docker!\")"}'
```

Expected response:

```json
{"output": "Hello from Docker!\n"}
```

---

## Supported Languages

| Key  | Language   | Runner image      |
|------|------------|-------------------|
| `py` | Python     | compiler-python   |
| `js` | JavaScript | compiler-node     |
| `java`| Java      | compiler-java     |
| `c`  | C          | compiler-c        |
| `cpp`| C++        | compiler-cpp      |
| `rs` | Rust       | compiler-rust     |
| `rb` | Ruby       | compiler-ruby     |
| `jl` | Julia      | compiler-julia    |

---

## API Reference

### `GET /health`

Health check — returns `200 OK` if the server is up.

### `POST /compile`

Run a code snippet.

**Request body (JSON):**

```json
{
  "language": "py",
  "code": "print('hello')"
}
```

**Response (success):**

```json
{ "output": "hello\n" }
```

**Response (runtime/compile error):**

```json
{ "error": "<stderr from the compiler/runtime>" }
```

---

## Security

Each run gets:

- `--network none` — zero network access from user code
- `--memory 128m` — capped RAM
- `--cpus 0.5` — capped CPU
- `--pids-limit 64` — prevents fork bombs
- Source mounted as **read-only** (`-v /tmp/...:/code:ro`)
- 15-second timeout; container is force-killed if exceeded

---

## Running Locally (without Docker for the API itself)

```bash
cd backend
pip install -r requirements.txt
python server.py
```

> Docker must still be installed on the machine for code execution to work.

---

## Updating the Flutter App

See [Point Flutter to Custom Backend](https://github.com/lakshya076/code_compiler#2-point-flutter-to-your-custom-backend)
