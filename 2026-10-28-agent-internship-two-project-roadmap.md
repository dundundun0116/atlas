# Agent 开发实习双项目升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use task-by-task execution with tests, experiment artifacts, and review checkpoints. Do not put planned capabilities or unverified metrics into the resume.

**Goal:** 将 Atlas 与 CompassRAG 打造成一组互补的 Agent 开发项目，分别证明长程 Agent 的状态、记忆、协作与恢复能力，以及检索型 Agent 的工具决策、证据获取、成本控制和评测能力。

**Architecture:** CompassRAG 负责“单个问题如何搜索”：LLM 通过结构化工具调用选择路由、拆分子问题、调用多路检索、判断证据是否充分并决定停止或修复。Atlas 负责“长期任务如何持续执行”：任务状态进入带生命周期的图，Planner、Worker、Auditor 围绕共享状态图协作，服务通过 checkpoint、事件日志和幂等写入支持恢复。两个项目都使用 Pydantic 约束 LLM 输出，都有可回放的运行轨迹和受控评测，但不共用同一个项目故事。

**Tech Stack:** Python 3.12、OpenAI-compatible SDK、Pydantic v2、FastAPI、SQLAlchemy 2、SQLite/PostgreSQL、LangGraph、BM25S、Sentence Transformers/BGE-M3、NumPy、PyArrow、pytest、OpenTelemetry-compatible structured tracing。

---

## 求职定位

目标岗位是 Agent 开发、大模型应用、RAG/智能体平台和后端 Agent 基础设施实习。面试官最可能追问的不是页面长什么样，而是：

- Agent 为什么需要多步循环，而不是一次 prompt；
- 工具调用和状态变更如何约束，如何防止 LLM 直接破坏状态；
- RAG 何时检索、检索几轮、何时停止，如何识别证据不足；
- 多 Agent 如何共享信息，如何避免角色之间依赖隐藏对话；
- 服务中断、超时、重复请求和模型输出不合法时，系统如何恢复；
- 效果提升是否来自真实机制，而不是样本、预算或评测口径变化。

因此两项目的组合应该是：

| 项目 | 核心问题 | 面试标签 | 不能重复的边界 |
|---|---|---|---|
| CompassRAG | Agent 如何决定查什么、查几轮、何时停止 | Agentic RAG、Tool Calling、Query Planning、Evidence Verification、成本控制 | 面向单轮/单问题检索，不承担跨任务长期状态 |
| Atlas | 长程任务如何保存状态、协作、修订和恢复 | Agent Memory、Stateful Agent、Multi-Agent Orchestration、Checkpoint、Reliability | 面向跨子任务执行，不把检索召回作为主线 |

两项目共同体现一个方法论：**LLM 负责提出候选决策，确定性代码负责校验、执行、记录和恢复。**

## 最终理想化简历版本

以下内容是完成升级并拿到相应实验结果后的目标版本。括号中的数字必须替换成真实实验结果，未完成前不能直接写入简历。

### 项目一：CompassRAG

> **CompassRAG——面向多跳问答的检索决策型 Agent｜独立开发**  
> **技术栈：** Python、OpenAI SDK、Pydantic、LangGraph、BM25S、Sentence Transformers/BGE-M3、NumPy、PyArrow、结构化 Agent Tracing
>
> **项目描述：** 针对传统 RAG 固定“检索一次再生成”导致多跳证据缺失、无效检索浪费 token、证据不足却强行回答的问题，设计并实现一个以检索决策为核心控制流的 Agent。系统根据问题难度选择直答、单跳或多跳路径，动态生成检索计划，调用 BM25、稠密向量和实体关系图三类工具获取证据，并在每轮后判断证据是否足够，支持早停、追加查询和失败修复。
>
> **工具调用与检索规划：** 使用结构化输出约束路由器、查询分解器和证据判定器，统一封装检索工具的输入、输出、超时和错误类型；通过显式状态机管理 `plan → retrieve → verify → answer`，禁止模型绕过工具直接伪造证据，解析失败自动降级为安全的单轮检索。
>
> **多路检索与结构化索引：** 融合 BM25、BGE-M3 稠密检索和实体关系图 PPR 检索信号，使用 RRF 和近重去重形成候选证据集；实体图跨文档连接共享实体，补足纯相似度检索难以覆盖的第二跳证据。查询改写支持多跳子问题分解和 HyDE，所有查询变体保留来源和检索轮次。
>
> **证据闭环与成本控制：** 每轮根据原问题、已获得证据和缺失信息生成下一步查询；当证据满足回答条件时提前停止，达到轮数、token、延迟或费用上限时强制收敛。记录每次工具调用、检索轮次、输入输出 token、延迟、证据覆盖和停止原因，支持按题回放与失败归因。
>
> **评测结果：** 在 MuSiQue、2WikiMultiHopQA 和 HotpotQA 的固定子集上进行同题集消融，比较固定单轮 RAG、查询改写、实体图索引和自适应决策的增量；进一步统计答案 EM/F1、支撑段落覆盖率、全链路证据命中率、无答案拒答率、平均检索轮数、token 和单位问题成本。最终报告 `[EM/F1 提升]`、`[证据 full-hit 提升]` 和 `[成本/延迟变化]`，并提供逐题失败轨迹。

**建议口述主线：**

> CompassRAG 解决的是“Agent 应不应该继续查”的问题。普通 RAG 把检索当作固定前置步骤，而我的系统把检索当作 Agent 工具，让模型先判断问题难度，再决定查询计划和搜索深度。每一轮之后系统都检查证据是否真的支持原问题，不满足就生成下一条查询，满足就提前结束。模型只负责提出路由和查询，工具层负责召回，确定性评测负责验证证据覆盖，所以可以同时讨论效果、成本和失败原因。

### 项目二：Atlas

> **Atlas——面向长程任务的可恢复多智能体记忆与协作系统｜独立开发**  
> **技术栈：** Python、FastAPI、Pydantic、SQLAlchemy 2、SQLite/PostgreSQL、OpenAI SDK、LangGraph、结构化 Agent Tracing
>
> **项目描述：** 针对长程 Agent 在跨子任务执行中遗忘约束、沿用过期决策、重复执行和多角色状态不同步的问题，设计并实现一套以图状态为唯一真源的记忆与协作系统。系统将目标、子任务、约束、决策、事实和产出物统一建模，结合结构化结转、动态上下文组装和可恢复的多 Agent 编排，使任务能够跨上下文边界持续执行。
>
> **状态模型与记忆一致性：** 设计带生命周期、来源、版本和证据的执行状态图；节点支持完成、过期和被新结论替代，旧状态不静默删除，而是保留作废原因、接替关系和变更事件。所有 LLM 提议先通过 Pydantic schema、规则校验和事务写入，避免模型直接产生非法边、循环依赖或矛盾状态。
>
> **多智能体协作：** 实现 Planner → Worker → Auditor → Repair 的显式状态机。Planner 生成带理由的执行路线，Worker 产生可审计产出，Auditor 按约束和证据核验，只有核验失败时才进入有限次数修订；角色之间不传递隐藏对话，而是通过共享状态图读写决策、事实和产出物。
>
> **上下文工程与恢复：** 在子任务边界或上下文预算阈值触发结转，将原始轨迹转换为结构化状态；组装器按任务阶段动态分配计划约束、有效状态、近期记录和风险提示的 token 预算，并过滤已过期或被替代信息。服务记录 checkpoint、事件日志、模型调用和状态版本，支持超时重试、重复请求幂等和进程中断后的断点恢复。
>
> **可靠性与评测：** 在固定长程任务上对比全历史、叙事摘要和图状态记忆方案，统计任务正确率、约束违规率、过期信息误用率、恢复成功率、上下文 token、调用次数、延迟和成本；通过故障注入验证模型空响应、工具超时、服务重启、重复提交和 Auditor 反复打回时系统仍能进入可解释终态。

**建议口述主线：**

> Atlas 解决的是“Agent 做了几十步之后还记不记得前面的状态”的问题。普通摘要适合压缩文字，但不适合保存约束、决策变更和产出之间的关系，所以我把执行状态建模成带生命周期的图。模型不能直接写图，只能提交结构化操作，系统校验后事务落库。多 Agent 通过共享状态图协作，服务用 checkpoint 和幂等事件保证中断恢复。这个项目的重点不是调用多少模型，而是如何让 Agent 变成一个可恢复、可审计的状态ful系统。

## 目标技术栈的取舍

### 两个项目共同保留

- **Python 3.12：** 便于共享 Agent、检索、评测和服务代码，面试时可以深挖实现细节。
- **OpenAI-compatible SDK：** 模型端点可替换；模型调用统一进入 telemetry，不绑定某一家服务商。
- **Pydantic v2：** 约束路由、工具参数、状态操作、评测结果和错误对象，是两个项目可靠性的共同基础。
- **结构化 tracing：** 记录 Agent 步骤、工具调用、token、延迟、停止原因和状态变化，而不是只打散乱文本日志。

### CompassRAG 专属

- **LangGraph：** 只承担 `plan/retrieve/verify/answer` 控制流和循环边；工具输出、证据 provenance 和评测数据由项目自己的 schema 管理。
- **BM25S + Sentence Transformers/BGE-M3：** 分别承担词法和稠密检索；不引入 LlamaIndex/Haystack 作为主抽象，以保持消融和遥测可控。
- **实体关系图 + PPR：** 作为检索信号，不把项目包装成“数据库项目”。
- **PyArrow/Parquet：** 用于语料和离线评测产物，避免把 JSONL 当成核心技术。

### Atlas 专属

- **FastAPI + SQLAlchemy 2：** 将图记忆和编排服务化，支持事务、checkpoint、幂等和恢复。
- **SQLite/PostgreSQL：** SQLite 用于单机和测试，PostgreSQL 用于多会话部署；JSONL 只保留导出和回放格式。
- **LangGraph + 自有状态仓储：** LangGraph 只负责角色控制流；任务状态、事件、checkpoint 和业务约束由 Atlas 自有 repository 管理，避免框架 checkpoint 与业务状态形成双重真源。

## 联合升级计划

### 阶段 0：建立双项目面试边界

**目标：** 先把两个项目的职责、数据流和共同接口定下来。

**产出：**

- `docs/agent-projects-boundary.md`：两项目边界、共享术语和面试问答。
- Atlas 架构图：API → 编排 → 状态图 → 持久化 → 评测。
- CompassRAG 架构图：Agent 控制流 → 工具注册 → 多路检索 → 证据验证 → 评测。
- 两项目统一的运行事件字段：`run_id`、`step_id`、`parent_step_id`、`agent_role`、`tool_name`、`input_hash`、`latency_ms`、`token_usage`、`outcome`。

**验收：** 能用一句话解释区别：CompassRAG 管“问题内搜索”，Atlas 管“任务间状态”。

### 阶段 1：CompassRAG 工具调用化

**目标：** 把当前 Agent 主循环升级为一个可解释的工具调用 Agent。

**建议文件：**

- Create: `/Users/szx/compassrag/compassrag/agent/state.py`
- Create: `/Users/szx/compassrag/compassrag/agent/tools.py`
- Create: `/Users/szx/compassrag/compassrag/agent/schemas.py`
- Create: `/Users/szx/compassrag/tests/test_agent_tools.py`
- Modify: `/Users/szx/compassrag/compassrag/agent.py`
- Modify: `/Users/szx/compassrag/compassrag/decision/planner.py`
- Modify: `/Users/szx/compassrag/compassrag/decision/sufficiency.py`

**实施项：**

- [ ] 定义 `RouteDecision`、`SearchPlan`、`SearchRequest`、`EvidenceCheck`、`StopReason` 和 `AnswerTrace`。
- [ ] 将 BM25、dense、KG 检索封装成统一工具协议：输入 query、top_k、filters；输出 chunk id、score、source、retriever、rank。
- [ ] 为每次检索保存 `query_source`，区分原问题、子问题、sufficiency 生成查询和 HyDE 变体。
- [ ] 让 planner 和 sufficiency checker 只输出 schema，不允许返回自由文本控制命令。
- [ ] 为每个工具增加超时、空结果、索引缺失和参数非法的显式错误。
- [ ] 将 `scripted` 与 `decision` 统一到同一状态图，通过开关关闭决策节点，而不是维护两套逻辑。
- [ ] 使用 LangGraph 表达 `plan/retrieve/verify/answer` 控制流；状态中保存经过 schema 校验的 `AgentState`，证据和 provenance 仍由项目自己的数据结构管理。

**验收：** 模型输出格式错误时能降级到单轮检索；工具超时不导致整题崩溃；每个答案可以列出完整的计划、查询、工具、证据和停止原因。

### 阶段 2：CompassRAG 证据闭环与安全收敛

**目标：** 让 Agent 不只是“搜到了就回答”，而是能判断证据是否支持结论。

**建议文件：**

- Create: `/Users/szx/compassrag/compassrag/diagnose/verifier.py`
- Create: `/Users/szx/compassrag/compassrag/diagnose/repair.py`
- Create: `/Users/szx/compassrag/tests/test_diagnosis.py`
- Modify: `/Users/szx/compassrag/compassrag/agent.py`

**实施项：**

- [ ] 让答案生成器同时返回结构化 claim 列表和证据引用，而非只有答案字符串。
- [ ] 对每条 claim 检查是否存在支持它的 chunk；支持不足时输出 `insufficient_evidence`。
- [ ] 将失败分成 query miss、evidence gap、source conflict、answer unsupported、tool failure 五类。
- [ ] 为每类失败定义有限修复动作：改写查询、补检索、切换检索视图、拆分子问题或拒答。
- [ ] 设置全局上限：最大轮数、最大 token、最大延迟、最大估算费用；触顶后输出可解释的 `budget_exhausted`。
- [ ] 对 null query 或证据为空的题，要求 Agent 选择拒答/unknown，不允许强行生成事实。

**验收：** 失败轨迹能解释“为什么继续查、为什么停止、为什么拒答”；修复循环有明确上限；空证据和矛盾证据不会被误标为成功回答。

### 阶段 3：CompassRAG 评测升级

**目标：** 把已有消融表升级为 Agent 开发岗位最看重的过程评测。

**建议文件：**

- Create: `/Users/szx/compassrag/compassrag/eval/traces.py`
- Create: `/Users/szx/compassrag/compassrag/eval/replay.py`
- Create: `/Users/szx/compassrag/tests/test_trace_replay.py`
- Modify: `/Users/szx/compassrag/compassrag/eval/metrics.py`
- Modify: `/Users/szx/compassrag/scripts/eval_qa.py`

**实施项：**

- [ ] 保留已有同题集消融：naive、rewrite、KG、decision、full。
- [ ] 增加过程指标：计划正确率、工具调用成功率、证据 full-hit、claim support rate、无答案拒答准确率、平均轮数、提前停止率、失败修复成功率。
- [ ] 做同预算对照：固定 token/延迟预算，比较“多查”是否真的带来更高答案质量。
- [ ] 做按跳数分层统计：1-hop、2-hop、3-hop、4-hop；不要只报总平均。
- [ ] 每个 run 写完整配置快照、模型信息、语料版本和随机种子。
- [ ] 保存逐题 trace，支持 replay，不重新请求模型也能重算证据和停止指标。
- [ ] 把当前 50 题结果作为历史小样本，不再扩写成稳定结论；正式结果至少使用固定三基准、重复运行和明确 n 值。

**验收：** 一条失败记录可在本地重放；一张结果表同时包含质量、证据、成本和可靠性；所有对照使用相同题集和预算。

### 阶段 4：Atlas 持久化、事件和 checkpoint

**目标：** 让 Atlas 从内存图 + JSONL 快照变成真正的 stateful Agent 后端。

**建议文件：**

- Create: `/Users/szx/atlas/atlas/storage/models.py`
- Create: `/Users/szx/atlas/atlas/storage/repository.py`
- Create: `/Users/szx/atlas/atlas/storage/events.py`
- Create: `/Users/szx/atlas/tests/test_repository.py`
- Modify: `/Users/szx/atlas/atlas/memory/engine.py`
- Modify: `/Users/szx/atlas/atlas/service.py`
- Modify: `/Users/szx/atlas/pyproject.toml`

**实施项：**

- [ ] 用 SQLAlchemy 2 定义 session、node、edge、event、checkpoint、run_metric 表。
- [ ] 所有图状态写入通过 repository，禁止业务层直接改持久化对象。
- [ ] 用事务保证节点、边、supersede 关系和事件一起提交。
- [ ] 建立 `session_id + idempotency_key` 唯一约束，重复请求返回原结果。
- [ ] checkpoint 保存当前任务状态、状态图版本、待执行角色、重试次数和上下文摘要版本。
- [ ] SQLite 开启 WAL；数据库层保留 PostgreSQL 可迁移能力。
- [ ] JSONL 保留为离线导出、调试和 replay 输入，不再作为主存储。

**验收：** 进程杀死后可以从 checkpoint 继续；重复请求不产生重复节点；事务失败不会留下孤立边；事件日志可以重建关键状态变化。

### 阶段 5：Atlas 显式编排和故障恢复

**目标：** 把三角色流程升级成可恢复的 Agent runtime。

**建议文件：**

- Create: `/Users/szx/atlas/atlas/orchestration/state_machine.py`
- Create: `/Users/szx/atlas/atlas/orchestration/policies.py`
- Create: `/Users/szx/atlas/tests/test_state_machine.py`
- Modify: `/Users/szx/atlas/atlas/orchestration.py`
- Modify: `/Users/szx/atlas/eval/atlas_agent.py`

**实施项：**

- [ ] 定义 `PLANNING`、`EXECUTING`、`AUDITING`、`REPAIRING`、`PAUSED`、`COMPLETED`、`FAILED`。
- [ ] 定义合法转移、触发条件和转移原因；非法转移写审计事件并拒绝。
- [ ] 为 Planner、Worker、Auditor 调用增加 request id、超时、有限重试和 fallback。
- [ ] 只重试模型调用，不重复提交已成功的状态操作。
- [ ] Auditor 输出 `pass`、`repair_required` 或 `fatal`，必须引用状态节点或证据。
- [ ] 修订最多一次；超过上限进入 FAILED，并保留失败原因。
- [ ] 注入进程中断、模型空响应、工具超时、数据库冲突和重复提交故障，验证恢复路径。
- [ ] 使用 LangGraph 表达 Planner、Worker、Auditor 和 Repair 的控制流；不启用其业务 checkpoint 作为第二状态源，所有恢复数据统一进入 Atlas repository。

**验收：** 所有中断场景都能进入 COMPLETED、PAUSED 或 FAILED 这三个可解释终态之一；没有无限重试和隐式状态丢失。

### 阶段 6：Atlas 上下文质量和状态治理

**目标：** 将“记忆增强”从数据结构升级为有来源、可冲突检测、可解释的上下文策略。

**建议文件：**

- Create: `/Users/szx/atlas/atlas/context/provenance.py`
- Create: `/Users/szx/atlas/tests/test_context_policy.py`
- Modify: `/Users/szx/atlas/atlas/context/extractor.py`
- Modify: `/Users/szx/atlas/atlas/context/assembler.py`
- Modify: `/Users/szx/atlas/atlas/context/carryover.py`

**实施项：**

- [ ] 为 Fact、Constraint、Decision、Artifact 增加 source、actor、confidence、valid_from、valid_until、verified_at。
- [ ] 组装前检查互斥约束、过期决策和未验证产出；冲突进入风险区，不默默覆盖。
- [ ] 记录每次组装实际 token、过滤节点、过滤原因和关键约束缺席情况。
- [ ] 让 LLM 只能生成候选操作，规则层负责节点类型、边类型、权限、依赖环和 supersede 合法性。
- [ ] 提取失败时执行有限重试、规则摘要和最近记录降级，并把降级原因写进 trace。
- [ ] 增加同一图快照 + 同一预算下的确定性组装测试。

**验收：** 过期信息不会进入有效计划区；冲突状态会被显式提示；同一输入可复现同一组装结果；每次降级都有可追踪原因。

### 阶段 7：两项目统一可观测性与面试证据

**目标：** 让两个项目都能回答“这一步为什么发生、花了多少、失败在哪里”。

**实施项：**

- [ ] 统一 JSON trace schema，但两个项目分别保存自己的业务字段。
- [ ] 记录模型名称、prompt/completion/reasoning token、延迟、重试、工具调用、错误和停止原因。
- [ ] 增加成本估算器，按模型配置计算单题/单任务成本。
- [ ] 生成两类报告：逐题/逐任务 trace 和聚合指标表。
- [ ] 每个项目准备至少三个故障案例，形成“现象 → 根因 → 修复 → 回归测试 → 结果”的口述材料。
- [ ] 所有简历数字注明样本量、对照组、预算和运行次数。

## 最终简历中的技术栈写法

### CompassRAG

`Python、OpenAI SDK、Pydantic、LangGraph、BM25S、BGE-M3、实体关系图检索、Agentic RAG、Tool Calling、Structured Tracing`

### Atlas

`Python、FastAPI、Pydantic、SQLAlchemy、SQLite/PostgreSQL、OpenAI SDK、LangGraph、Agent Memory、Multi-Agent Orchestration、Checkpoint Recovery`

只保留真正实现并通过测试或实验验证的名称。`pytest`、JSONL、Streamlit、MemoryArena 不放在技术栈第一行；它们分别属于测试、导出、调试和评测说明。

## 面试准备清单

### CompassRAG 必答问题

- 为什么不能固定检索一轮？什么时候 direct route 会误判？
- BM25、dense、KG 三路信号分别解决什么问题？为什么用 RRF？
- HyDE 可能引入幻觉，为什么仍然使用？如何限制它的影响？
- 证据充分度判定器如何避免“模型说充分但其实没有支持”？
- 如何保证模型输出的 query 不会导致无限检索或费用失控？
- full-hit、claim support rate、答案 F1 之间是什么关系？
- 当前实验中哪些结果是正面的，哪些是负面的或口径受限的？

### Atlas 必答问题

- 为什么执行状态要用图，而不是一段摘要或向量检索？
- 为什么 LLM 不能直接修改状态图？
- supersede、expire、done 的区别是什么？
- 多 Agent 为什么通过共享图协作，而不是直接传消息？
- 事务、幂等和 checkpoint 分别解决什么问题？
- Auditor 重试和 Worker 修订如何避免死循环？
- 服务重启后怎样保证不会重复执行或丢失已经写入的状态？

### 两项目共同追问

- 什么情况下应该用 LangGraph，什么情况下自己写状态机？
- 如何区分模型问题、检索问题、状态管理问题和评测问题？
- 如果 token 预算减半，系统哪些行为会改变？
- 如果模型换成另一个 OpenAI-compatible endpoint，哪些模块不需要修改？

## 交付门槛

- **CompassRAG：** 工具调用有 schema，检索循环有上限，答案有证据引用，失败可分类，trace 可回放，消融和过程指标可复现。
- **Atlas：** 状态写入有事务，事件不可变，任务有 checkpoint，重复请求幂等，角色编排有合法状态转移，故障注入可恢复。
- **共同：** 原有测试全部通过；新增测试覆盖 schema、工具失败、预算触顶、恢复、重试和 replay；简历所有数字都有实验记录。
- **口述：** 每个项目能讲 90 秒主线，并能在 10 分钟内回答架构、失败、成本、评测和取舍问题。
