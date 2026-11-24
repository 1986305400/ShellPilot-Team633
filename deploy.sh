#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'ShellPilot: %s\n' "$*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || fail 'Run this script on Linux.'
(( EUID != 0 )) || fail 'Run as a regular user, without sudo.'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
runtime="$PWD/.spt-runtime"
revision=fb5997274ec0d0b4d8fe067eb55f87c0ad405478
model_file=qwen2-1_5b-instruct-q4_k_m.gguf
model_sha=f521a15453fd7f820e8467f4a307c99e44f5ab9cc24273d2fe67cd7cb1288f05

for tool in python3 curl sha256sum; do
    command -v "$tool" >/dev/null || fail "Missing $tool; see README."
done
nginx=$(command -v nginx || true)
export SPT_NGINX_EXE="${SPT_NGINX_EXE:-${nginx:-/usr/sbin/nginx}}"
"$SPT_NGINX_EXE" -v || fail 'Nginx unavailable; see README.'
export SPT_AGENT_PORT=8000 SPT_BACKEND_PORT=18081 SPT_FRONT_PORT=18080 SPT_STALE_PORT=18082
export SPT_ENABLE_FIXTURE=1 SPT_MODEL_MODE=local SPT_MODEL_NAME=qwen2-1.5b
export SPT_MODEL_URL=http://127.0.0.1:8081/v1/chat/completions
unset SPT_MODEL_API_KEY

python3 - <<'PY'
import socket
import sys
if sys.version_info < (3, 11):
    sys.exit('ShellPilot: Python 3.11 or later is required.')
for port in (8081, 8000, 18080, 18081, 18082):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', port))
        except OSError as exc:
            sys.exit(f'ShellPilot: port {port} is not available ({exc}).')
PY

mkdir -p -- "$runtime/models"
if [[ ! -x .spt-venv/bin/python ]]; then python3 -m venv .spt-venv; fi
.spt-venv/bin/python -m pip install -r requirements-shellpilot.lock
chmod u+x spt

if [[ -z ${SPT_LLAMA_SERVER:-} ]]; then
    source_dir="$runtime/llama.cpp"
    build_dir="$runtime/build"
    for tool in git cmake c++; do
        command -v "$tool" >/dev/null || fail "Missing $tool; see README."
    done
    if [[ ! -d "$source_dir/.git" ]]; then
        git clone --depth 1 --branch b12007 https://github.com/ggml-org/llama.cpp.git "$source_dir"
    fi
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$revision" ]] || fail 'Unexpected llama.cpp revision.'
    if [[ ! -x "$build_dir/bin/llama-server" ]]; then
        cmake -S "$source_dir" -B "$build_dir" -DCMAKE_BUILD_TYPE=Release \
            -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_UI=OFF \
            -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
        cmake --build "$build_dir" --target llama-server -j "${SPT_BUILD_JOBS:-2}"
    fi
    SPT_LLAMA_SERVER="$build_dir/bin/llama-server"
fi
[[ -x "$SPT_LLAMA_SERVER" ]] || fail "llama-server is not executable: $SPT_LLAMA_SERVER"
"$SPT_LLAMA_SERVER" --version

model=${SPT_MODEL_FILE:-"$runtime/models/$model_file"}
verify_model() { printf '%s  %s\n' "$model_sha" "$1" | sha256sum -c --status; }
if [[ ! -f "$model" ]]; then
    [[ -z ${SPT_MODEL_FILE:-} ]] || fail "Local GGUF not found: $model"
    url="https://huggingface.co/Qwen/Qwen2-1.5B-Instruct-GGUF/resolve/main/$model_file"
    curl -fL --retry 2 --connect-timeout 20 -o "$model.part" "$url"
    verify_model "$model.part" || fail 'Downloaded GGUF checksum does not match.'
    mv -- "$model.part" "$model"
else
    verify_model "$model" || fail "GGUF checksum does not match: $model"
fi

model_pid= agent_pid=
cleanup() {
    if [[ -n "$agent_pid" ]]; then
        kill -TERM "$agent_pid" 2>/dev/null || true
        wait "$agent_pid" || true
    fi
    if [[ -n "$model_pid" ]]; then
        kill -TERM "$model_pid" 2>/dev/null || true
        wait "$model_pid" || true
    fi
}
wait_health() {
    local url=$1 pid=$2 log=$3 i
    for ((i=0; i<120; i++)); do
        if ! kill -0 "$pid" 2>/dev/null; then break; fi
        if curl -fsS --noproxy '*' --max-time 1 -o /dev/null "$url" 2>/dev/null; then return; fi
        sleep 1
    done
    tail -n 20 -- "$log" >&2
    fail "Service did not become ready: $url (see $log)"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$SPT_LLAMA_SERVER" -m "$model" --alias "$SPT_MODEL_NAME" --host 127.0.0.1 \
    --port 8081 -c 2048 -np 1 -ngl 0 -t 4 >"$runtime/model.log" 2>&1 &
model_pid=$!
wait_health http://127.0.0.1:8081/health "$model_pid" "$runtime/model.log"

.spt-venv/bin/python agent_server.py >"$runtime/agent.log" 2>&1 &
agent_pid=$!
wait_health http://127.0.0.1:8000/health "$agent_pid" "$runtime/agent.log"

printf '\nShellPilot ready. Keep this terminal open.\nIn a second terminal, enter this repository and run ./spt --check-model\nLogs: %s\n\n' "$runtime"
wait -n "$model_pid" "$agent_pid" || true
fail "A service exited; see logs in $runtime."
