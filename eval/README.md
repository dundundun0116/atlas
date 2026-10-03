# eval/ —— M1 评测环境

对齐 MemoryArena 官方协议的评测接入层。当前进度：long_context 对照（formal_reasoning_math 域）。

## 组成

- `lite_memory_server.py`：轻量 memory server（端口 8000）。复用官方 `LongContextMemorySystem` 类（importlib 按文件加载，绕开 `memory_systems/__init__.py` 的重型依赖），端点与官方 `memory/server.py` 一致。
- `configs/`：运行配置。模型与 base_url 用 `<MODEL>` / `<BASE_URL>` 占位，启动前填好。

## 运行步骤

```bash
# 0) API key（agent 与 judge 共用；OpenAI 兼容端点）
export OPENAI_API_KEY=sk-...            # 可选: export OPENAI_BASE_URL=https://...

# 1) env server（端口 8001；math 环境创建时即初始化 judge，所以必须先有 key）
cd external/MemoryArena && .venv/bin/python env/env_server.py

# 2) memory server（端口 8000；long_context 基线也需要，MemoryClient 初始化时会连）
.venv/bin/python eval/lite_memory_server.py

# 3) 跑 math 域 long_context 对照（40 篇 paper，顺序执行、断点续跑）
cd external/MemoryArena && .venv/bin/python run_math.py -c /Users/szx/atlas/eval/configs/math_longcontext.json
```

结果落在 `output.json_output_dir`（默认 `runs/json/math/long_context_<model>/result.jsonl`），评测脚本 `env/env_systems/formal_reasoning_env/eval.py` 产出 `all_results.json`（SR / PS / passrate@k）。

## 说明

- venv 在 `external/MemoryArena/.venv`（uv 创建，Python 3.12），只装了 math 域必需依赖；其他域再按需补。
- `runs/` 不入库（.gitignore）。
- 断点续跑：runner 按 paper 粒度跳过已有完整 result.jsonl 的任务，中断后重跑即续。
