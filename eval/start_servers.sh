#!/usr/bin/env bash
# 启动 M1 评测所需服务：env server (:8001) + lite memory server (:8000)
# 凭据从 eval/.env 读取；日志写在 runs/ 下。重复运行前先 pkill 旧进程。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/eval/.env"; set +a
export OPENCODE_COMPAT=1
export OPENCODE_SESSION="atlas-eval-math"
export PYTHONPATH="$ROOT/eval/opencode_compat${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$ROOT/runs"
cd "$ROOT/external/MemoryArena"

nohup .venv/bin/python env/env_server.py > "$ROOT/runs/env_server.log" 2>&1 &
echo "env_server  pid=$!  log=runs/env_server.log"
nohup .venv/bin/python "$ROOT/eval/lite_memory_server.py" > "$ROOT/runs/memory_server.log" 2>&1 &
echo "memory_server pid=$!  log=runs/memory_server.log"
