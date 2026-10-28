# Atlas

<p align="center">
  <img src="docs/architecture.png" alt="Atlas 功能示意图" width="880">
</p>

![License](https://img.shields.io/badge/license-MIT-blue) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Tests](https://img.shields.io/badge/tests-passing-brightgreen)

**带时效的图状态记忆 + 结转式上下文管理 + 多智能体编排，面向 long-horizon 任务。**

> Long-horizon agents forget. Text compression drops structure and exact values; semantic retrieval fragments decision chains. Atlas keeps execution state in a time-aware graph, carries it over into the model context, and lets multiple roles coordinate through that single shared graph.

Atlas 在 [MemoryArena](https://arxiv.org/abs/2602.16313) 基准上做量化验证：官方 runner 不做任何改动，记忆系统与 agent 侧均为可插拔实现，A/B 对照只差一行 config。

## Why

长任务必然超出上下文窗口，历史必须压缩；压缩丢状态 → agent 违反早期约束、重复犯已修正的错误、目标漂移。现有两类方案各有硬伤：

- **总结式压缩**：自由文本摘要留不住结构、公式、精确值；
- **语义检索式记忆**（Mem0 / GraphRAG / HippoRAG 一类）：按相似度捞取，在相互依赖任务上打碎决策链。

执行状态需要一个**不依赖语义相似度、也不依赖自由文本摘要**的结构化承载——这是 Atlas 的出发点。

## 核心设计

### 1. 图状态记忆（`atlas/memory/`）

六类节点（Goal / Subtask / Constraint / Decision / Artifact / Fact）+ 五类边 + **时效生命周期**：`active → done / superseded / expired`。**作废不删除**——记录原因与接替指针（`superseded_by`），决策变迁整链可回溯；Decision 节点自带 `reason` / `evidence`；每条状态带 `source` / `confidence` / `valid_until` / `verified_at` 来源信息。状态落 SQLAlchemy（SQLite WAL，可迁移 PostgreSQL），JSONL 保留为导出与回放格式。

### 2. 结转式上下文管理（`atlas/context/`）

- **结转触发**：子任务边界优先（依赖链中间绝不切），上下文用量 80% 兜底；
- **LLM 提取器**：压缩前先把原始记录**结构化写入图**——严格 JSON、逐操作校验（白名单 / 防环 / 去重 / 缺 reason 拒绝）、支持 `supersede` 作废更新，解析失败自动降级；
- **四区组装器**：计划与约束 / 已完成工作摘要 / 近期原始记录 / 踩坑提示，动态预算（默认 8k tokens），稳定前缀；**规则式查账**——活跃约束 / 决定缺席即报错，零额外 LLM 成本。

### 3. 多智能体编排（`atlas/orchestration/`）

**Planner → Worker → Auditor** 固定三段式：Planner 定路线（写 Decision）→ Worker 解题（写 Artifact 草稿）→ Auditor 核验（写 Fact 疑虑）→ 仅当判"需修订"时 Worker 修订一次（修订稿作废草稿、记原因）。**协调全经共享图**：角色读 `state_view`、写 `role_note`，不靠对话历史传递状态。config 一行开关，失败逐级降级（编排 → 单发 → 官方 agent）。

控制流用 LangGraph 表达（`build_control_flow_graph`），但**业务状态不走框架 checkpoint**：任务状态、事件、恢复数据全部由 Atlas 自己的 repository 管理，避免双重真源。

### 4. 可恢复运行时（`atlas/runtime.py`）

`AgentRuntime` 是唯一对外门面：`create_task / advance_task / pause_task / resume_task / get_state / get_trace / get_report / replay`。所有写请求带 `request_id` + 幂等键，重复请求返回原结果；任务可在 `PAUSED` 后**换进程从 checkpoint 继续**，且只会继续一次；`replay` 只凭事件与 checkpoint 重建生命周期，并能检测事件序列损坏（seq 非单调、引用未知节点、终态再变更）。HTTP 层（`atlas/api/`）是这层门面的薄壳。

### 5. 评测与可观测（`atlas/evaluation/`、`atlas/observability/`）

- 结构化 trace：角色、工具、模型、token、延迟、重试、状态版本、停止原因，可按任务回放；
- 三类长程 fixture（多跳累积 / 新旧来源冲突 / 需修订）× 三种记忆策略（全历史 / 叙事摘要 / 图状态）对照，支持五类故障注入与离线 replay；
- **口径纪律**：离线脚手架结果只验证指标，真实模型结果才可作为效果数字，详见 [docs/evaluation.md](docs/evaluation.md)。

## 快速开始

```bash
# 30 秒看运行时：建任务 → 推进 → 暂停 → 换连接恢复 → 回放
.venv/bin/python examples/quickstart.py

# 完整研究链路：规划 → 研究工具取证据 → 执行 → 核验打回 → 修订 → 恢复
.venv/bin/python examples/research_agent.py

# 图状态记忆演示（零 API 凭据）：脚本化提取器跑一段旅行规划迷你会话，
# 展示结转提取 → 四区组装 → 图快照导出
.venv/bin/python -m atlas.demo                  # 产物：runs/demo/graph.html 与 graph.svg

# 可视化演示页：同一场景，交互式观察结转过程与图状态
.venv/bin/streamlit run atlas/demo_app.py

# 图快照导出（任何一次会话落盘的图都能渲染）
.venv/bin/python -m atlas.cli export runs/graphs/<user_id>.jsonl --out snapshot.html
```

跑评测（MemoryArena 基准，需要 API 凭据）：

```bash
eval/start_servers.sh                                     # 起 env server + memory server
eval/run_math.sh                                          # 官方基线臂
eval/run_math.sh eval/configs/math_atlas.json             # Atlas 臂（A/B 只差这一行）
eval/run_math.sh eval/configs/math_atlas_orch.json        # + 多智能体编排
external/MemoryArena/.venv/bin/python eval/summarize.py runs/json/math --out runs/table.md
```

环境与凭据说明见 [eval/README.md](eval/README.md)。

## 评测设计

- **基准**：MemoryArena `formal_reasoning_math` 域——多会话相互依赖的论文推理任务，一篇论文对应一个记忆会话，后面的子任务依赖前面的结论；
- **协议**：官方 runner 原样使用，抽样子集（固定种子、可复现），各臂共用同一清单；
- **对照**：官方全历史基线 / Atlas（图状态记忆）/ Atlas + 编排 / 叙事摘要简化基线（MAGE 式）；
- **指标**：官方口径——progress score（子任务正确率）、pass rate（末题正确占比）、memory_length（上下文 token，省 token 度量）。

另有一套面向长程运行的离线评测（记忆策略对照 + 故障注入 + replay）：

```bash
.venv/bin/python scripts/run_runtime_eval.py --mode offline       # 脚手架验证口径
.venv/bin/python scripts/run_runtime_eval.py --mode real \        # 真实模型（需 OPENAI_*）
    --model <name> --repeats 3
```

两种模式的口径与复现方式见 [docs/evaluation.md](docs/evaluation.md)：**只有 real 模式的结果可以作为效果数字**。

## 项目结构

```
atlas/
├── memory/            # 图引擎：六类节点 / 五类边 / status 生命周期 / JSONL 持久化
├── context/           # LLM 结转提取器 + 四区组装器 + 规则查账 + 结转触发
├── service.py         # MemoryArena 接入（AtlasMemorySystem，官方接口等价物）
├── orchestration/     # 三段式编排 + 状态机 + 角色策略（langgraph 只表达控制流）
├── runtime.py         # 对外门面：任务生命周期 / 恢复 / 回放
├── storage/           # SQLAlchemy 仓储 + 不可变事件 + checkpoint
├── tools/             # ResearchTool 协议 + CompassRAG 适配器
├── api/               # FastAPI 薄壳（任务 API）
├── observability/     # 结构化 trace
├── evaluation/        # 三 fixture × 三策略 + 故障注入 + replay
├── demo.py            # 离线演示（CLI）
├── demo_app.py        # 离线演示（streamlit 页）
├── export.py / cli.py # 图快照导出（SVG / HTML，零依赖）
└── mage_baseline.py   # 叙事摘要简化基线（对照组）
examples/              # 可运行示例：quickstart / research_agent
scripts/               # 评测入口（run_runtime_eval.py）
eval/                  # 评测脚手架：memory server（单端口分派四臂）/ 配置 / 兼容层
tests/                 # 单元测试（TDD，271 个）
docs/                  # 架构图（D2 源码 + SVG）
notes/                 # 论文精读笔记
SURVEY.md              # 方法调研
```

## Roadmap

- [x] 可恢复运行时：持久化 / 事件 / checkpoint / 幂等 / 任务 API / trace / replay
- [x] 三类长程 fixture × 三种记忆策略对照 + 五类故障注入（离线口径）
- [ ] 真实模型评测（固定任务集、预算、重复次数）——这是简历效果数字的前提
- [ ] 角色消融（Planner / Worker / Auditor 各自的贡献拆分）
- [ ] 更多评测域与更长的会话（省 token 卖点的主战场）

## License

[MIT](LICENSE)
