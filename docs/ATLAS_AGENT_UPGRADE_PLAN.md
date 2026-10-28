# Atlas 完整 Research Agent 升级计划

## 定位

Atlas 的最终定位是一个面向长程研究任务的可恢复多智能体 Agent，而不是单纯的记忆库或编排 demo。

它负责把用户的复杂目标拆成多个研究子任务，调用外部工具获得证据，维护跨子任务共享状态，检查中间结论，发现问题后重新规划，并在中断后从 checkpoint 继续执行。

与 CompassRAG 的边界是：CompassRAG 负责“把一个问题查清楚”，Atlas 负责“把一项复杂任务做完”。Atlas 通过稳定的 `ResearchTool` 接口调用 CompassRAG，不复制 CompassRAG 的检索实现。

## 最终目标架构

```text
用户目标
  |
  v
FastAPI / Agent API
  |
  v
Task Runtime: session / idempotency / timeout / checkpoint
  |
  v
LangGraph 控制流
  Planner -> Worker -> Auditor -> Repair / Finish
  |             |          |
  |             |          +-- 约束、证据、产出核验
  |             +------------- 调用 ResearchTool / CompassRAG
  +--------------------------- 任务拆解、预算和依赖
  |
  v
Atlas State Graph（唯一业务状态源）
  Goal / Subtask / Constraint / Decision / Fact / Artifact
  |
  v
SQLAlchemy Repository
  nodes / edges / events / checkpoints / run_metrics
```

LangGraph 只负责控制流；Atlas repository 负责业务状态、事件、版本和恢复数据。不得同时让 LangGraph checkpoint 和 Atlas repository 成为状态真源。

## 最终简历版本

> **Atlas——面向长程研究任务的可恢复多智能体 Agent｜独立开发**  
> **技术栈：** Python、FastAPI、Pydantic、LangGraph、SQLAlchemy 2、SQLite/PostgreSQL、OpenAI SDK、Agent Memory、Checkpoint Recovery、Structured Tracing
>
> 针对长程 Agent 在跨子任务执行中遗忘约束、沿用过期决策、重复执行和多角色状态不同步的问题，设计并实现一套可恢复的多智能体研究系统。系统将目标、子任务、约束、决策、事实和产出物统一建模，由 Planner、Worker、Auditor 和 Repair 角色围绕共享状态图协作，并通过 CompassRAG 检索工具完成证据获取。
>
> 设计带生命周期、来源、版本和证据的执行状态图；旧决策不静默覆盖，而是记录作废原因、接替关系和变更事件。LLM 只能提交经过 Pydantic schema 校验的候选操作，规则层负责权限、依赖环、过期状态和事务写入。
>
> 使用 LangGraph 管理任务控制流：Planner 拆分研究目标并分配预算，Worker 调用检索工具生成阶段产出，Auditor 依据约束和证据核验，失败时进入有限次数 Repair 并自动作废旧产出；角色之间通过共享状态图协作，不依赖隐藏对话历史。
>
> 在子任务边界或上下文预算阈值触发结转，将轨迹转换为结构化状态并按阶段动态组装上下文。通过事件日志、幂等键、checkpoint、超时重试和故障注入，支持进程中断恢复、重复请求去重和失败任务回放。
>
> 在固定长程任务上对比全历史、叙事摘要和图状态记忆方案，统计任务完成率、约束违规率、过期信息误用率、恢复成功率、上下文 token、调用次数、延迟和成本。最终数字必须注明样本量、预算、运行次数和对照组。

## 核心模块

### 1. ResearchTool 接口

Atlas 不直接实现 BM25、向量检索或实体图检索，而是定义统一的研究工具协议。

建议请求字段：

```json
{
  "session_id": "session-001",
  "subtask_id": "S3",
  "query": "查询内容",
  "budget_tokens": 4000,
  "max_rounds": 2,
  "idempotency_key": "S3-attempt-1"
}
```

建议响应字段：

```json
{
  "answer": "阶段性结论",
  "evidence": [
    {"source": "...", "chunk_id": "...", "quote": "...", "score": 0.91}
  ],
  "confidence": 0.84,
  "retrieval_rounds": 2,
  "stop_reason": "evidence_sufficient",
  "trace_id": "trace-001"
}
```

Atlas 收到响应后，将结论写成 `Fact` 或 `Artifact`，将来源写成 evidence/provenance 边，并由 Auditor 决定是否可信、是否需要补查。

### 2. 状态图与生命周期

核心节点：

- `Goal`：用户总目标；
- `Subtask`：可执行研究子任务及依赖；
- `Constraint`：用户要求、格式限制和禁止事项；
- `Decision`：Planner 的路线和选择，必须带 reason/evidence；
- `Fact`：已核验事实或检索结论；
- `Artifact`：阶段报告、草稿和最终报告。

状态必须支持 `active`、`done`、`superseded`、`expired`。作废不能删除旧节点，必须记录原因、替代节点和事件。

### 3. 任务状态机

```text
PLANNING -> EXECUTING -> AUDITING -> COMPLETED
                         |             ^
                         v             |
                      REPAIRING -------+
                         |
                         v
                       FAILED
```

任务还可以进入 `PAUSED`，表示等待外部输入、预算补充或人工处理。每次转移必须记录触发原因、当前状态版本和 actor。

## 分阶段实施

### 阶段 A：抽象研究工具边界

**目标：** 让 Atlas 能把 CompassRAG 当作工具调用，并保留证据来源。

**文件：**

- Create: `atlas/tools/protocol.py`
- Create: `atlas/tools/compassrag.py`
- Create: `tests/test_research_tool.py`
- Modify: `atlas/orchestration.py`
- Modify: `eval/atlas_agent.py`

**任务：**

- [ ] 定义 `ResearchRequest`、`EvidenceItem`、`ResearchResult` 和 `ResearchTool` protocol。
- [ ] 实现本地 CompassRAG adapter；HTTP adapter 作为第二种实现，保持上层不变。
- [ ] 工具错误统一为 timeout、invalid_request、no_evidence、budget_exhausted、upstream_error。
- [ ] 给每次工具调用分配 `trace_id` 和幂等键。
- [ ] 将 ResearchResult 写入 Atlas 的 Fact/Artifact 节点，并保存 evidence provenance。

**验收：** 使用 fake ResearchTool 可以跑完整 Planner → Worker → Auditor 流程；工具失败不会破坏已有状态；同一幂等键重复执行不会重复写入。

### 阶段 B：持久化状态、事件和 checkpoint

**目标：** 把内存图和 JSONL 快照升级为可恢复的 Agent runtime。

**文件：**

- Create: `atlas/storage/models.py`
- Create: `atlas/storage/repository.py`
- Create: `atlas/storage/events.py`
- Create: `tests/test_repository.py`
- Modify: `atlas/memory/engine.py`
- Modify: `atlas/service.py`
- Modify: `pyproject.toml`

**任务：**

- [ ] 用 SQLAlchemy 2 定义 session、node、edge、event、checkpoint 和 run_metric 表。
- [ ] 所有节点、边、supersede 和事件写入放在同一事务中。
- [ ] 建立 `session_id + idempotency_key` 唯一约束。
- [ ] checkpoint 保存任务状态、图版本、待执行角色、重试次数和上下文版本。
- [ ] SQLite 开启 WAL；保留 PostgreSQL 兼容的数据访问层。
- [ ] JSONL 保留为导出、迁移和离线 replay 格式。

**验收：** 进程被杀后可以从 checkpoint 继续；重复请求不增加节点；事务失败不会留下孤立边或半条 supersede 关系。

### 阶段 C：LangGraph 编排和可靠性策略

**目标：** 把当前三角色流程变成有上限、可恢复、可解释的任务状态机。

**文件：**

- Create: `atlas/orchestration/state_machine.py`
- Create: `atlas/orchestration/policies.py`
- Create: `tests/test_state_machine.py`
- Modify: `atlas/orchestration.py`

**任务：**

- [ ] 定义 `PLANNING`、`EXECUTING`、`AUDITING`、`REPAIRING`、`PAUSED`、`COMPLETED`、`FAILED`。
- [ ] 用 LangGraph 描述节点和边，业务状态仍从 repository 读取。
- [ ] Planner、Worker、Auditor 调用增加 request id、超时、有限重试和 fallback。
- [ ] 重试模型调用时复用幂等键，不重复提交成功的状态操作。
- [ ] Auditor 只返回 `pass`、`repair_required` 或 `fatal`，且必须引用状态节点或证据。
- [ ] Repair 最多执行一次，超过上限进入 FAILED。
- [ ] 每个状态转移成功后保存 checkpoint。

**验收：** 正常、打回、空响应、超时、数据库冲突、重复提交和进程中断都能进入 COMPLETED、PAUSED 或 FAILED 终态；没有无限重试。

### 阶段 D：上下文结转和状态治理

**目标：** 让 Atlas 的记忆能力能解释“为什么保留、过滤或替代一条状态”。

**文件：**

- Create: `atlas/context/provenance.py`
- Create: `tests/test_context_policy.py`
- Modify: `atlas/context/extractor.py`
- Modify: `atlas/context/assembler.py`
- Modify: `atlas/context/carryover.py`

**任务：**

- [ ] 为 Fact、Constraint、Decision、Artifact 增加 source、actor、confidence、valid_from、valid_until 和 verified_at。
- [ ] 组装前检查互斥约束、过期决策和未验证产出。
- [ ] 记录实际 token、被过滤节点和过滤原因。
- [ ] LLM 只生成候选状态操作，规则层负责节点、边、权限和依赖校验。
- [ ] 提取失败时执行有限重试、规则摘要和近期记录降级。
- [ ] 增加相同图快照和预算下的确定性组装测试。

**验收：** 过期信息不会进入有效计划区；冲突会显式告警；同一输入产生稳定组装结果；每次降级都能在 trace 中定位。

### 阶段 E：任务 API、观测和回放

**目标：** 把 Atlas 变成可被客户端调用和排查的 Agent 服务。

**文件：**

- Create: `atlas/api/schemas.py`
- Create: `atlas/api/routes.py`
- Create: `atlas/observability/tracing.py`
- Create: `tests/test_api_contract.py`
- Modify: `eval/atlas_memory_server.py`

**任务：**

- [ ] 暴露创建任务、推进任务、查看状态、恢复 checkpoint、读取 trace 和获取最终报告的 API。
- [ ] 所有请求包含 session id、request id 和幂等键。
- [ ] 统一返回参数错误、状态冲突、模型超时、存储失败和不可恢复失败。
- [ ] 记录角色、工具、模型、token、延迟、重试、状态版本和停止原因。
- [ ] 增加健康检查和存储自检。

**验收：** API 重复请求安全；一次失败任务能定位到具体角色、模型调用和数据库事件；服务重启后能继续任务。

### 阶段 F：完整 Research Agent 评测

**目标：** 证明 Atlas 不只是状态框架，而是能完成长程任务。

**任务：**

- [ ] 设计至少三类长程研究 fixture：多子问题、存在冲突来源、需要修订的任务。
- [ ] 对比全历史、叙事摘要、Atlas 图状态三种记忆策略。
- [ ] 增加编排开关和角色消融：单 Agent、Planner/Worker、Planner/Worker/Auditor。
- [ ] 注入进程中断、工具超时、模型空响应、重复提交和过期决策等故障。
- [ ] 统计任务完成率、约束违规率、过期信息误用率、恢复成功率、上下文 token、延迟、调用次数和成本。
- [ ] 保存每次运行的配置、状态快照、事件日志、上下文和最终报告，支持离线 replay。

**验收：** 任意一次失败可回放；结果表同时包含任务质量、可靠性、上下文和成本；简历数字注明样本量和重复次数。

## 面试必讲案例

1. **为什么不用一段摘要：** 摘要无法稳定表达“旧决策被新决策替代”的关系，图节点和 supersede 事件可以保留状态变化。
2. **为什么 LLM 不能直接写图：** LLM 输出是候选操作，规则层必须校验边类型、依赖环、权限、版本和幂等。
3. **为什么不直接使用 LangGraph checkpoint：** LangGraph 只表达控制流；业务状态需要查询、审计、事务和跨服务访问，所以由自有 repository 维护。
4. **如何恢复：** 读取最近 checkpoint，检查状态版本和幂等事件，重试未完成的模型调用，跳过已经提交的状态变更。

## 完成标准

- CompassRAG 可作为 ResearchTool 返回带来源的结构化证据。
- Atlas 具备任务拆解、工具调用、状态图、Auditor 修订、checkpoint 和最终报告生成能力。
- 所有状态写入事务化，所有请求可幂等，所有故障有可解释终态。
- 测试覆盖工具错误、状态冲突、恢复、重复提交和修订上限。
- 评测报告同时包含任务质量、状态可靠性、token、延迟和成本。
