#!/usr/bin/env bash
# 跑 math 域 long_context 基线（opencode go 兼容层 + 断点续跑）
# 前置：eval/start_servers.sh 已启动两个服务
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/eval/.env"; set +a
export OPENCODE_COMPAT=1
export OPENCODE_SESSION="atlas-eval-math"
export PYTHONPATH="$ROOT/eval/opencode_compat${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/external/MemoryArena"
exec .venv/bin/python run_math.py -c "$ROOT/eval/configs/math_longcontext.json"
