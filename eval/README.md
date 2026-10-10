# eval/ —— M1 评测环境

对齐 MemoryArena 官方协议的评测接入层。当前进度：math 域双臂就绪——`long_context` 基线 + `atlas` 图状态记忆（M2 接入层，决策 #11–#14）。

## 组成

- `atlas_memory_server.py`：memory server（端口 8000），按 `memory_system_name` 分派：`long_context` 复用官方 `LongContextMemorySystem`；`atlas` 用 `atlas/service.py` 的 `AtlasMemorySystem`（图引擎 + LLM 结转提取器 + 四区组装，预算默认 8000 tokens）。端点与官方 `memory/server.py` 一致，A/B 只差 config 一行。`ATLAS_EXTRACTOR_MODEL`（默认 deepseek-v4.1-flash）、`ATLAS_BUDGET_TOKENS` 可用环境变量覆盖。
- `opencode_compat/sitecustomize.py`：opencode go 兼容层。该端点要求客户端标识自身（User-Agent）并携带每会话稳定的 `x-opencode-session` 头（路由与前缀缓存优化，见 https://opencode.ai/docs/go/）。补丁经 `PYTHONPATH` 自动加载，仅在 `OPENCODE_COMPAT=1` 时生效，给所有 `openai.OpenAI` 客户端注入这两个头。
- `start_servers.sh`：一键起 env server（:8001）+ memory server（:8000），从 `eval/.env` 读凭据，服务日志在 `runs/`。
- `run_math.sh`：math 域跑分（带兼容层，断点续跑）；`eval/run_math.sh [config.json]`，默认 long_context 基线。
- `configs/math_longcontext.json` / `configs/math_atlas.json`：双臂运行配置（judge 口径两边一致：`judge_result_in_memory=true`，决策 #14）。
- `.env`：API 凭据（gitignored，不入库）。

## 运行步骤（当前后端：opencode go / deepseek-v4.1-flash）

```bash
# 1) 起服务（含兼容层环境变量）
eval/start_servers.sh
# 确认：curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/docs   # 200

# 2) 跑 math 域（抽样子集：默认 n=10、seed=20261010，共 95 子任务）
eval/run_math.sh                                      # long_context 基线臂
eval/run_math.sh eval/configs/math_atlas.json         # Atlas 臂（同一子集清单，保证可比）
#    可用 SUBSET_N / SUBSET_SEED 覆盖；子集清单写入 runs/subset_*.txt，两臂共用

# 3) 评测结果
#    runs/json/math/long_context_deepseek-v4.1-flash/<paper>/result.jsonl（基线逐 paper）
#    runs/json/math/atlas/<paper>/result.jsonl（Atlas 臂逐 paper）
#    runs/json/math/atlas_orch/<paper>/result.jsonl（编排臂逐 paper）
#    汇总成 Markdown 表（复用官方 eval.py 口径，并在臂目录写官方 all_results.json）：
#    external/MemoryArena/.venv/bin/python eval/summarize.py runs/json/math --out runs/table.md
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
- **judge 口径（决策 #14）**：两臂 `judge_result_in_memory=true`（runner 每步都判分，该开关决定判分是否写进记忆条目）。2026-10-10 首次跑的那篇（`2312.05134_part_2`，judge=false 口径）已移到 `runs/stale_judge_false/` 存档，重跑前不删除会因断点续跑被跳过、导致口径混杂。
- **Atlas 臂说明（决策 #11–#13）**：每子任务边界结转（wrap 到新子任务时先结转上一个：LLM 提取入图 + 叙事摘要）；提取器只在语义状态上做判断（规则负责子任务注册/完成/判分），输出严格 JSON，解析失败自动降级为规则摘要并把错误打进 `runs/memory_server.log`；组装失败（查账/溢出/环）降级为计划区，保证跑分不中断。
- **提取器模型（实测选型）**：默认 `mimo-v2.6-flash`（同任务 33s、推理 ~400 token、JSON 合规）；`deepseek-v4.1-flash` 在长记录段上推理失控（8000 token 全被推理吃光、单次 17 分钟、常以连接错误告终），已弃用于提取器。可用 `ATLAS_EXTRACTOR_MODEL` 覆盖；`ATLAS_EXTRACTOR_MAX_TOKENS`（默认 8000）与 `ATLAS_BUDGET_TOKENS`（默认 8000）同理。
- **图状态快照（决策 #10）**：Atlas 臂每次 wrap 把图落盘到 `runs/graphs/<user_id>.jsonl`（`ATLAS_GRAPH_DUMP_DIR` 可改，设空串关闭）；导出成可视化快照：
  ```bash
  .venv/bin/python -m atlas.cli export runs/graphs/<user_id>.jsonl --out snapshot.html   # 或 .svg
  ```
- **单篇冒烟**：`SMOKE_PAPER=<paper_name> eval/run_math.sh eval/configs/math_atlas.json`（结果隔离在 `runs/json/math_smoke/`，不污染正式跑分）。
- **编排臂（M3.5，决策 #15–#17）**：`eval/run_math.sh eval/configs/math_atlas_orch.json`——同一 memory server（`memory_system_name=atlas_orch` 仅让结果落独立目录），`agent.orchestration=true` 使入口脚本把 runner 的 MathAgent 替换为编排 agent（Planner → Worker → Auditor → 必要时 Worker 修订）；角色经 `/memory/state_view`（读图）与 `/memory/role_note`（写图）协调，结论/疑虑落成节点（actor 标角色），修订作废草稿。推理开关：机械性 JSON 调用（提取器、Planner/Auditor）默认关推理（`ATLAS_EXTRACTOR_THINKING=on` 可恢复）；解题（Worker/修订）保持推理，预算 `ATLAS_WORKER_MAX_TOKENS`（默认 24000），空正文时自动关推理重试一次。
