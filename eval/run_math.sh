#!/usr/bin/env bash
# 跑 math 域 long_context 基线（抽样子集，决策 #7；opencode go 兼容层 + 断点续跑）
# 前置：eval/start_servers.sh 已启动两个服务；可用 SUBSET_N/SUBSET_SEED 覆盖抽样参数
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a; source "$ROOT/eval/.env"; set +a
export OPENCODE_COMPAT=1
export OPENCODE_SESSION="atlas-eval-math"
export PYTHONPATH="$ROOT/eval/opencode_compat${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/external/MemoryArena"
exec .venv/bin/python -u "$ROOT/eval/run_math_subset.py" "$ROOT/eval/configs/math_longcontext.json"
