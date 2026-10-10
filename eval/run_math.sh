#!/usr/bin/env bash
# 跑 math 域（抽样子集，决策 #7；opencode go 兼容层 + 断点续跑）
# 用法：eval/run_math.sh [config.json]
#   默认 long_context 基线；Atlas 臂：eval/run_math.sh eval/configs/math_atlas.json
# 前置：eval/start_servers.sh 已启动两个服务；可用 SUBSET_N/SUBSET_SEED 覆盖抽样参数
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${1:-$ROOT/eval/configs/math_longcontext.json}"
set -a; source "$ROOT/eval/.env"; set +a
# 本地服务必须绕过代理：shell 若带 HTTP(S)_PROXY（Clash 127.0.0.1:7897）而 NO_PROXY
# 不含 0.0.0.0，对本地服务的请求会被误发到代理，Clash 一挂跑分全报 ProxyError
# （2026-10-10 中断故障的根因）。这里补齐旁路名单。
export no_proxy="0.0.0.0,127.0.0.1,localhost${no_proxy:+,$no_proxy}"
export NO_PROXY="$no_proxy"
export OPENCODE_COMPAT=1
export OPENCODE_SESSION="atlas-eval-math"
export PYTHONPATH="$ROOT/eval/opencode_compat${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/external/MemoryArena"
exec .venv/bin/python -u "$ROOT/eval/run_math_subset.py" "$CFG"
