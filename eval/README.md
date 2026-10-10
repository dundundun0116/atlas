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

# 2) 跑 math 域 long_context 对照（40 篇 paper，顺序执行、断点续跑）
eval/run_math.sh

# 3) 评测结果
#    runs/json/math/long_context_deepseek-v4.1-flash/<paper>/result.jsonl（逐 paper）
#    python external/MemoryArena/env/env_systems/formal_reasoning_env/eval.py 汇总 all_results.json
```

## 说明

- venv 在 `external/MemoryArena/.venv`（uv 创建，Python 3.12），只装了 math 域必需依赖；其他域再按需补。
- `runs/` 不入库（.gitignore）。
- 断点续跑：runner 按 paper 粒度跳过已有完整 result.jsonl 的任务，中断后重跑即续。
- 历史：NVIDIA NIM 免费档实测延迟 ~98s/请求且 HTTP/2 不稳，已弃用；opencode go 实测 ~2s/请求（简单调用）。
- opencode go 注意事项：直连即可（无需代理）；请求必须带 `x-opencode-session` 头（缺失会 400 `MissingSessionID`），兼容层已处理。
