# eval/ —— M1 评测环境

对齐 MemoryArena 官方协议的评测接入层。当前进度：long_context 对照（formal_reasoning_math 域）。

## 组成

- `lite_memory_server.py`：轻量 memory server（端口 8000）。复用官方 `LongContextMemorySystem` 类（importlib 按文件加载，绕开 `memory_systems/__init__.py` 的重型依赖），端点与官方 `memory/server.py` 一致。
- `opencode_compat/sitecustomize.py`：opencode go 兼容层。该端点要求客户端标识自身（User-Agent）并携带每会话稳定的 `x-opencode-session` 头（路由与前缀缓存优化，见 https://opencode.ai/docs/go/）。补丁经 `PYTHONPATH` 自动加载，仅在 `OPENCODE_COMPAT=1` 时生效，给所有 `openai.OpenAI` 客户端注入这两个头。
- `start_servers.sh`：一键起 env server（:8001）+ lite memory server（:8000），从 `eval/.env` 读凭据，服务日志在 `runs/`。
- `run_math.sh`：math 域 long_context 基线跑分（带兼容层，断点续跑）。
- `configs/math_longcontext.json`：运行配置（模型与端点已填，换后端只改 `.env` 与这里）。
- `.env`：API 凭据（gitignored，不入库）。

## 运行步骤（当前后端：opencode go / deepseek-v4.1-flash）

```bash
# 1) 起服务（含兼容层环境变量）
eval/start_servers.sh
# 确认：curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/docs   # 200

# 2) 跑 math 域 long_context 对照（抽样子集：默认 n=10、seed=20261010，共 95 子任务）
eval/run_math.sh
#    可用 SUBSET_N / SUBSET_SEED 覆盖；子集清单写入 runs/subset_*.txt，Atlas 对照跑用同一清单保证可比

# 3) 评测结果
#    runs/json/math/long_context_deepseek-v4.1-flash/<paper>/result.jsonl（逐 paper）
#    python external/MemoryArena/env/env_systems/formal_reasoning_env/eval.py 汇总 all_results.json
```

## 说明

- venv 在 `external/MemoryArena/.venv`（uv 创建，Python 3.12），只装了 math 域必需依赖；其他域再按需补。
- `runs/` 不入库（.gitignore）。
- 抽样协议：决策 #7（每域固定 n、固定种子）；当前 math 域 n=10、seed=20261010，清单 `runs/subset_formal_reasoning_math_n10_seed20261010.txt`。多次重复（多 seed）留到 M3 的 Atlas-vs-基线对比阶段。
- 断点续跑：runner 按 paper 粒度跳过已有完整 result.jsonl 的任务，中断后重跑即续。
- 数据加载：`run_math_subset.py` **本地缓存直载优先**（`~/.cache/huggingface/hub/datasets--ZexueHe--memoryarena/`），HF hub 仅作后备——直连偶发失败时 datasets 会走有 bug 的缓存回退路径（ValueError），本地直载完全绕开。
- 历史：NVIDIA NIM 免费档实测延迟 ~98s/请求且 HTTP/2 不稳，已弃用；opencode go 实测 ~2s/请求（简单调用）。
- opencode go 注意事项：直连即可（无需代理）；请求必须带 `x-opencode-session` 头（缺失会 400 `MissingSessionID`），兼容层已处理。
- 后台运行请用 `python -u`（脚本已带），否则输出被块缓冲看不到进度。长跑建议 `nohup eval/run_math.sh >> runs/math_run.log 2>&1 &`，并确认进程不挂在会被回收的会话下（2026-10-10 曾随会话回收中断一次）。
- **代理注意**：本地服务地址一律用 `127.0.0.1`（config 已改），且 `run_math.sh`/`start_servers.sh` 会把 `0.0.0.0,127.0.0.1,localhost` 加入 `no_proxy`。原因是 shell 常带 `HTTP(S)_PROXY=127.0.0.1:7897`（Clash），而 `0.0.0.0` 不在系统 `NO_PROXY` 里，对本地服务的请求会被误发到代理——Clash 一挂，跑分全部报 ProxyError（2026-10-10 中断故障根因）。
