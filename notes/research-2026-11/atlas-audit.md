# Atlas 机制现状审计（供调研报告引用）

> 审计对象：`/Users/szx/atlas`（只读，未修改任何代码/测试）。审计时间：2026-11-04。
> 约定：只写代码里能直接看到的东西；每条结论 = "证据 + 能证明什么 / 不能证明什么"。凡代码里没有的，写"未找到证据"，不推测设计意图。
> 行号基准：当前工作区 HEAD 状态（`atlas/` 共 6.5k 行核心代码）。

## 0. 一句话结论 + 现状速览表

**一句话**：Atlas 的"机制骨架"在代码里基本都能找到（7 状态机、只追加事件、四区组装、图记忆失效语义、五类工具错误），但**每条机制都只在某一条路径上接线**：状态机+checkpoint 只在 `/tasks/*` 运行时路径，四区组装只在 MemoryArena memory-server 路径，两者没有汇合；而统一预算、重试策略、LangGraph 控制流、`expire()` 在生产路径上是**死代码**。

| 能力 | 有无 | 证据（file:line） | 覆盖范围 | 差距 |
|---|---|---|---|---|
| 7 状态任务状态机 + 合法转移表 | 有 | `atlas/orchestration/state_machine.py:23-55` | 全路径（`/tasks/*` 与测试）；eval 编排臂每子任务一个内存实例 `eval/atlas_agent.py:119` | 状态不落在独立表，只在 checkpoint JSON 里 |
| 非法转移拒绝 + 留痕 | 有 | `state_machine.py:158-173`、`178-182`、`220-230` | 有 machine 注入的路径 | `/orchestrate` 未注入 machine，`_transition` 全为 no-op `orchestrator.py:163-171`、`routes.py:166` |
| checkpoint | 有（三处写） | `runtime.py:124,153,180` → `service.py:299-320` | 仅 advance/pause/resume | 编排内部步、节点写入、工具调用都不写 checkpoint |
| pause/resume | 有 | `runtime.py:164-189`、`state_machine.py:163-166` | `/tasks/*` | 恢复只回状态机，不回上下文（无 pending 工具调用/计划快照） |
| 只追加事件日志 | 代码纪律级 | `atlas/storage/events.py:12-18`、`repository.py:584-614` | 全路径写库都经 `_emit_event` | 无 DB 级不可变约束；`seq` 用 max+1 生成，可重复 |
| replay | 部分 | `events.py:41-74`、`81-139`、`runtime.py:278-309` | 只重建节点生命周期状态 | 图本体从 `nodes` 表/JSONL 快照重建，不是从事件重放 |
| 幂等（请求级） | 有 | `runtime.py:330-348`、`repository.py:392-438` | create/advance/pause/resume | `/orchestrate`、`/research` 的模型/图写入不走这套 |
| 幂等（写节点级） | 有 | `repository.py:189-197`、`models.py:54` | 调用方显式传 key 时 | 编排器的 `role_note` 没有该参数 `api/graph.py:30-54` |
| 图记忆 schema（6 节点/5 边/4 状态） | 有 | `atlas/memory/schema.py:14-65` | 内存引擎 + repository 双实现 | 两套实现字段一致但无共享校验层 |
| 失效/被替代语义 | 有 | `schema.py:34-38`、`repository.py:273-322`、`engine.py:83-124` | 引擎/仓储层可用 | 生产只有一个调用点：修订稿作废草稿 `api/graph.py:41-50`；`expire()` 无调用者 |
| 四区上下文 | 有 | `atlas/context/assembler.py:26-32`、`70-118` | 仅 `AtlasMemorySystem`（memory-server）与离线评测 | task runtime / orchestrator 完全不用 |
| 结转 carryover | 有 | `atlas/context/carryover.py:16-45`、`55-93` | 仅 memory-server 路径 | 与 checkpoint 无关，进程内内存状态 |
| 固定三段式编排 | 有 | `orchestrator.py:173-277` | API / eval / 集成三条路径 | 无 DAG、无 step、无依赖调度 |
| 统一预算 Budget | 仅有类 | `orchestration/policies.py:59-115` | 只在测试中实例化 | 生产从未构造，`_charge` 恒为 no-op `orchestrator.py:361-369` |
| 角色重试/超时策略 | 仅有类 | `policies.py:32-38`、`119-167` | 只在 `tests/test_state_machine.py` 调用 | `orchestrator._call` 无重试无超时 `orchestrator.py:409-413` |
| LangGraph 控制流 | 有实现、无生产调用 | `state_machine.py:268-301` | 仅 `tests/test_state_machine.py:328-360` | 不挂在任何 runtime/API 路径 |
| 工具失败五分类 | 有 | `tools/protocol.py:13-20`、`compassrag.py:119-128` | 全部 ResearchTool 调用 | 分类后上层只记 trace，不重试不降级 |
| trace | 有（两套） | `observability/tracing.py:19-33,83-125`、`repository.py:442-454` | runtime 走 Tracer sink；routes 手写 `record_trace` | 编排角色调用无 trace（只有最终 answer 进图） |
| provenance | 有（只读审计） | `context/provenance.py:18-70`、`131-161` | 组装期与 `get_state` 读取时 | 不写库、不阻断写入；缺 source 只进风险段 |
| 评测判分 | 有（过宽） | `evaluation/harness.py:96-132,411-424`、`integration/merged_e2e.py:244-266` | 离线自建 fixture | 见第 9 节 |

## 1. 状态机与状态真源

**状态与转移**：7 个状态 `PLANNING/EXECUTING/AUDITING/REPAIRING/PAUSED/COMPLETED/FAILED`（`state_machine.py:23-30`），进行中 4 个、终态 2 个（`33-39`）。合法转移是显式字典（`42-55`）：`PAUSED` 可回到任意进行中状态，`COMPLETED/FAILED` 无出边。触发词 9 个（`58-68`），`resume` 目标由 `_paused_from` 决定（`163-166`）。

**非法转移**：不静默。`_resolve` 抛 `IllegalTransition`（`169-173`），`apply` 先 `record_violation`（写 `self.violations` + 审计钩子）再抛出（`178-182`）；测试断言越级 `work_done` 会留下 `violations[0]`（`tests/test_state_machine.py:63-72`）、终态拒绝一切触发词（`75-82`）、未知触发词抛 `ValueError`（`84-87`）。**能证明**：状态机本身是确定性代码，模型只能通过触发词推进。**不能证明**：违规在跨进程/重启后仍可查——`violations` 只在内存，不落 checkpoint（`234-243` 只序列化 state/paused_from/repairs/history）。

**修订预算**：默认 `max_repairs=1`（`126`），预算用尽时不再进 `REPAIRING` 而是直接 `FAILED` 并写 `repair_budget_exhausted`（`183-195`）；测试覆盖 `tests/test_state_machine.py:118-126`。但预算只在注入 `machine` 时生效，API 的 `/orchestrate` 不注入（`routes.py:166`）→ 该预算在生产编排路径不生效。

**状态唯一真源**：**不是** LangGraph checkpoint。`build_control_flow_graph` 的 docstring 明确"这里不挂 LangGraph 的 checkpointer"（`state_machine.py:268-273`），且该函数只有测试调用（`tests/test_state_machine.py:328-360` 三个用例），`grep` 全仓无生产调用点。实际真源是 **checkpoint 行的 `context_digest_version` 字段里的状态机 JSON**：`runtime._load` 每次请求都 `ServiceRuntime.resume()` 重建（`runtime.py:313-320`），`resume()` 从最新 checkpoint 读该字段并 `TaskStateMachine.from_checkpoint`（`service.py:346-356`），字段类型是 `Text`（`models.py:136-137`）。**能证明**：业务状态没有第二真源（没有框架 checkpointer，没有内存单例跨请求）。**不能证明**"图引擎+repository"这一表述严格成立——状态其实是"checkpoint 快照"承载，图节点另存 `nodes` 表。

另有一处字段错位：`CheckpointRow.graph_version` 写入的是**节点个数** `len(self.repository.nodes(...))`（`service.py:316`、`runtime.py:129`），而 `SessionRow.graph_version` 才是每次写 +1 的版本计数器（`repository.py:656-660`）。两者不是同一个量，报告引用 `checkpoint.graph_version` 时不能当成图版本。

## 2. checkpoint / pause / resume

**谁在何时写**：只有 `AgentRuntime` 的三个方法：`advance_task`（`runtime.py:124`）、`pause_task`（`153`）、`resume_task`（`180`）在状态机 `apply` 之后写一行 checkpoint。`StateMachine` 自带的 `checkpoint_sink` 钩子（`state_machine.py:128,142-143,214-215`）在生产未接线，唯一使用处是 `tests/test_research_tool.py:312`。**能证明**：checkpoint 是"转移即落盘"。**不能证明**"崩溃点精确恢复"——`_solve` 内部的 Planner/Worker/Auditor 步、`role_note` 写图、工具调用都不写 checkpoint，进程在中途死掉只能回到上一次状态转移。

**pause 后 resume 只能一次吗**：按"同一次暂停"是。`resume_task` 先查 checkpoint 存在（`runtime.py:171-173`），再要求当前状态必须是 `PAUSED`（`174-178`），否则 `StateConflict`；而 `resume` 成功后状态离开 `PAUSED`（`state_machine.py:196-199` 清空 `_paused_from`），所以第二次直接 409。测试 `tests/test_runtime_api.py:118-126` 正是这个断言。**但"只能一次"是每轮暂停一次，不是整条任务一次**：再次 `pause` 可以再 `resume`；且**同一幂等键**的重复 resume 会走 `_idempotent` 返回 `replayed=True` 而不是报错（`runtime.py:337-342`，测试 `tests/test_runtime_api.py:125-126`）。集成脚本也验证"换连接恢复一次 + 第二次被拒"（`integration/merged_e2e.py:271-305`）。

**恢复时怎么重建上下文**：不重建。`resume()` 只做两件事：`rebuild_engine`（从 `nodes`/`edges` 表反序列化图，`repository.py:546-573`）+ 从 checkpoint 反序列化状态机（`service.py:346-356`）。没有上下文摘要、没有 pending 工具调用、没有计划快照恢复——`CheckpointRow` 只有 `task_state/graph_version/pending_role/retry_count/context_digest_version`（`models.py:121-138`），且 `retry_count` 从未被传入非 0 值（`service.py:303` 默认 0，`runtime.py` 三处调用都不传）。

## 3. event log / replay / 确定性

**事件类型**：5 种 `add_node/link/supersede/expire/complete_subtask`（`events.py:12-18`），另有以方法名作 operation 的幂等结果事件（`runtime.py:345-347`）与 `trace`（`repository.py:442-445`）。`OPERATIONS` 元组只列 5 种，实际库里还会出现 `create_task/advance_task/pause_task/resume_task/trace`，说明**事件类型没有封闭枚举**。

**是否只追加**：代码层面是。唯一写入口是 `_emit_event`（`repository.py:584-614`），全仓没有 `delete`/`update` EventRow 的代码（`grep` 无命中）。但**只有代码纪律、没有存储层约束**：`EventRow` 的唯一约束是 `(session_id, idempotency_key)`（`models.py:104-107`），`(session_id, seq)` 只是普通索引。`seq` 由 `max(seq)+1` 计算（`repository.py:622-626`），并发写可能产生重复 seq；`replay_with_problems` 把重复 seq、边引用未知节点、终态节点再变更列为"损坏"（`events.py:93-137`）——即**损坏靠读时检测，不靠写时阻止**。测试覆盖 replay 的 timeline/states/checkpoint（`tests/test_runtime_api.py:165`）。

**replay 从事件还是快照**：**混合，且不是事件溯源**。`events.replay()` 只重建 `{node_id: {status, superseded_by, note}}`（`events.py:41-74`），`link` 完全不参与（`45` 注释明说）；`AgentRuntime.replay` 返回 timeline + 这份状态表 + 最新 checkpoint + traces（`runtime.py:278-309`），**不重建图内容**；图内容来自 `nodes` 表（`rebuild_engine`）或 JSONL dump（`service.py:200-208` 的 `_dump`）。另有一个完全不同的 replay：`atlas/evaluation/replay.py:40-52` 从 `run.json` 重算指标，与事件表无关。**能证明**：失败可离线复现（第二套）。**不能证明**：仅凭事件表能还原一张图。

镜像还有一条会丢信息的路径：`import_engine` 对已存在的节点 `continue`（`repository.py:469-471`），只有本次新插入的节点才补发生命周期事件（`505-522`）。所以节点在内存引擎里**首次镜像之后**才发生 supersede/expire，事件表不会记录，replay 结果与快照不一致。`_mirror` 在每次 `/memory/add` 与 `/memory/wrap_user_prompt` 都跑（`eval/atlas_memory_server.py:223-237`），该场景真实存在。

**确定性**：不是。可枚举的非确定来源：① `datetime.now()`（`state_machine.py:19-20`、`models.py:27-28`、`memory/engine.py:25-26`）写进转移记录、节点/事件/checkpoint 时间戳；② `uuid4` 生成 `event_id/checkpoint_id/metric_id/trace_id/request_id`（`repository.py:603`、`354`、`384`、`runtime.py:363`、`tracing.py:104`）；③ 模型采样：`Orchestrator._call` 直接调注入的 `llm(prompt)`（`orchestrator.py:409-413`），`grep temperature/seed/top_p` 在 `atlas/` 无任何设置；④ 网络：`HttpCompassRAGTool` 真实 HTTP（`compassrag.py:87-101`）；⑤ 事件排序：`events.replay` 只 `sorted(key=seq)`（`events.py:48`），seq 重复时顺序未定义，而 `replay_with_problems` 用 `(seq, event_id)` 且 `event_id` 是 uuid（`90`）——同一库两次 replay 可能给出不同顺序。

## 4. 幂等与重复执行

**幂等键是什么**：三套，互不相同。
1) API 请求键：`RequestMeta.idempotency_key`，服务端如果没传就**不生成**（`api/schemas.py:25-29`），`_idempotent` 仅在 `key` 非空时生效（`runtime.py:337-347`）。
2) 工具键：`build_idempotency_key(session, subtask, attempt)` → `"{session}:{subtask}:attempt-1"`（`tools/protocol.py:27-28`），默认 attempt=1，所以同一子任务的所有重试共用一个键（`protocol.py:106-113`）；走 HTTP 头 `X-Idempotency-Key`（`compassrag.py:83`）。
3) 节点写入键：`NodeRow(session_id, idempotency_key)` 唯一（`models.py:53-56`），`add_node` 命中即返回原行（`repository.py:189-197`）。

**作用在哪一层**：三层都有，但各管一段。repository 是最终防线（节点唯一约束 + `get_idempotent_result` 从事件表读原结果，`repository.py:392-407`）；runtime 是请求级串行化 + 事件存结果（`runtime.py:330-348`，并用 `threading.RLock` 按会话串行，`333-336`）；HTTP 层只透传（`routes.py:57-81`）。**跨进程有效**的依据是结果落在事件表（`repository.py:395` 注释 + `tests/test_api_contract.py:224-229` 重启后同键仍幂等）。

**重复请求会不会重复写 Fact**：
- `/tasks/{id}/research`：不会。`ServiceRuntime.note` 带 `idempotency_key=request.idempotency_key`（`routes.py:124`），测试 `tests/test_api_research.py:71-78` 断言两次 POST 返回同一 `fact_id` 且 fact 只有 1 条。
- 编排查路径：**会**。`Orchestrator._research` 的去重是进程内内存集合 `_research_written` + `_research_cache`（`orchestrator.py:142-143`、`300-311`、`333-335`），重启即失效；`role_note` 全链路没有 `idempotency_key` 参数（`api/graph.py:30-54`、`orchestrator.py:381-396`），Planner 的 Decision、Worker 的 Artifact、Auditor 的 Fact 都没有写入幂等。测试佐证的是**内存路径**：`tests/test_research_tool.py:270-282` 用同一个 Orchestrator 实例连续 solve 两次，断言 `tool.calls==1`、`research_notes()` 长度 1——证明的是缓存，不是持久幂等。
- 编排端点的实际防线是状态检查：只允许 `planning` 启动（`routes.py:156-161`），所以重复调用通常被 409 挡掉，而不是被幂等键挡掉。

`add_node` 的幂等是"同 key 返回旧行"，不做内容 diff；`link` 用 `(session,src,dst,type)` 去重（`repository.py:253-255`），没有幂等键。

## 5. 图记忆 schema 与失效语义

**节点/边**：6 类节点 `goal/subtask/constraint/decision/artifact/fact`（`schema.py:14-31`，id 前缀 G/S/C/D/A/F），4 种状态 `active/done/superseded/expired`（`34-38`），5 类边 `subtask_of/depends_on/constrains/decided_by/supersedes`（`41-46`）。边合法性是白名单 `LEGAL_EDGES`（`50-65`），例如 `CONSTRAINS` 只能从 Constraint 指向 Subtask/Goal/Decision，`SUPERSEDES` 只允许同类型（含 Artifact→Artifact，`62-64` 注释说明这是为修订稿加的）。

**字段**：`source/confidence/valid_from/valid_until/verified_at/meta`（`schema.py:80-85`）都在，且 `NodeRow` 列一一对应（`models.py:70-75`）。语义分工：`reason/evidence` 是 Decision 专属（`schema.py:78-79`，`engine.py:65-70` 强制 reason 非空）；`note` 存作废/过期原因；`superseded_by` 是接替指针（`76`）。`actor` 预留多写者（`73`）。

**Constraint/Decision/Fact/Artifact 各自语义**（只按代码里的强制约束说）：Decision 必须带 `reason`（`engine.py:66-67`、`repository.py:185-186`）；Constraint 必须挂在 Subtask/Goal/Decision 上才合法（`schema.py:53-55`）；Fact 没有强制 `evidence`，provenance 只要求 `source` 非空（`provenance.py:28-33`、`67-70`）；Artifact 未标 `verified_at` 即视为"未验证"，只进风险段不进"有效产出物"（`provenance.py:62-64`、`assembler.py:190-199`）。

**有没有表达"失效/被替代"**：有，且是双轨。① `supersede(old,new)`：old 置 `superseded`、写 `superseded_by`、`note=reason`，并建 `new --supersedes--> old` 边（`repository.py:273-306`；内存引擎同语义 `engine.py:83-115`，含 `supersede_with` 自动建同类型接替节点）。② `expire(node,reason)`：置 `expired` + 原因（`repository.py:308-322`）。③ 时效：`valid_until` + `is_stale`（`provenance.py:48-59`）。

但**生产接线极窄**：`grep` 全仓 `supersede` 的调用点只有三处，都是"修订稿作废草稿"（`atlas/api/graph.py:48`、`atlas/service.py:177`，以及 `integration/merged_e2e.py:189` 的重复实现）；`ServiceRuntime.expire` 有封装（`service.py:293-295`）但**没有任何调用者**；`valid_until` 只能由调用方传入（`service.py:276`）或由 LLM 提取器写入（`extractor.py:212`），`/research` 与 `/orchestrate` 都不传。也就是说：**"Fact 被新证据推翻"这条语义在代码里有能力、没有路径**；真正跑起来的失效只有 Artifact 的修订作废。`hints()` 会把作废/过期/曾答错的节点渲染成"踩坑提示"（`engine.py:196-212`），这是失效语义目前唯一的消费端。

## 6. 四区上下文组装

**四个区与字段名**：常量 `ZONE_PLAN/ZONE_SUMMARY/ZONE_RECENT/ZONE_HINTS`（`assembler.py:26-29`）→ ① 当前计划与约束（每步现渲染，不裁剪）② 已完成工作摘要 ③ 近期原始记录 ④ 踩坑与提示。计划区内容由 `_render_plan` 决定：目标、按 `dependency_order` 排的子任务（含依赖标注）、活跃约束、关键决定（带 reason）、环境事实、产出物（未验证会标注"（未验证）"）、末尾风险段（`143-201`）。**谁决定放什么**：纯代码规则 + `engine` 的 active 查询；`focus` 只影响事实/子任务的召回子集（`133-141`、`153-160`、`179-186`），不影响约束/决定。

**预算来源**：三个常量/参数，没有配置文件。份额 `DEFAULT_SHARES = 0.30/0.55/0.15`、地板 `DEFAULT_FLOORS = 300/400/120`（`31-32`）；总预算由调用方传 `budget_tokens`（`70-71`）。实际取值：memory-server 用 `ATLAS_BUDGET_TOKENS` 环境变量默认 8000（`eval/atlas_memory_server.py:47` → `service.py:28,56`）；离线评测策略用 800（`evaluation/policies.py:82`）并把地板改小（`89`）。

**预算分配算法**：计划区先算，若 `计划 + 地板总和 > 总预算` 直接抛 `PlanZoneOverflow`（`74-78`）——即**宁可失败也不裁计划区**；剩余预算若装得下三区自然需求就全额给（`90-91`），否则按份额分配并保底（`93-96`）。**有裁剪**：`_fit_items` 从头部（最旧）逐条弹出直到 ≤ allowance（`218-229`），`trimmed` 记录哪区被裁（`101-103`）。**有"计划重注入"**：`assemble` 每次都重新 `_render_plan()`（`72`），不做缓存；`render_plan` 也单独暴露给降级路径（`128-130`）。

**查账**：组装完成后必须能在文本里找到每个活跃 Constraint/Decision 的 `[ID]`，缺一个就抛 `AuditError`（`233-242`）。

**`carryover.py` 做什么**：两件事。① `CarryOverTrigger` 决定何时结转：自上次结转后出现新完成子任务，或缓冲用量 ≥ 阈值（默认 0.8）× 预算（`16-45`）。② `CarryOverManager.execute` 是"结转式压缩"三步：先调 `extractor` 把原始记录结构化写图（`62-65`）、再生成带全局步号的叙事摘要（LLM 摘要优先，缺失退化为逐行占位，`66-77`）、最后标注本次新完成子任务（只报一次，`78-92`）。调用方随后丢弃 `recent_raw`（`service.py:136`）。**关键点**：状态进图、摘要只负责叙事——摘要不承载状态（`carryover.py:5-6` 的 docstring 与代码一致）。

**调用方清单**（`grep -rn "assembler\|assemble"`）：只有 `atlas/service.py:65,148,191,198`（`AtlasMemorySystem`）、`atlas/evaluation/policies.py:87,134,141`、`tests/`。`atlas/runtime.py`、`atlas/orchestration/`、`atlas/api/` **零引用**。

## 7. orchestrator 执行路径（含调用图）

**是固定 ResearchWorkflow，不是通用 DAG**。`Orchestrator._solve` 是硬编码 4 步：Planner（写 Decision）→ 可选 Research（写 Fact）→ Worker（写 Artifact）→ Auditor（判 pass/revise/fatal）→ 条件修订一次（`orchestrator.py:173-277`）。**计划数据结构**：Planner 的输出是一个 JSON dict，只有 `{route, risks, reason}` 三个字符串字段（`29-42`、`179-195`），随后被压成一句 `route` 文本写进 Decision 节点（`190-194`）——**没有 step 列表、没有依赖字段、没有 step id**。

**没有的东西**（`grep` 在 `atlas/` 无命中）：`class Step`、`Scheduler`、`Dependency`（作为计划结构）、`PendingToolCall`、`replan`。`StepResult` 只存在于离线评测（`evaluation/harness.py:28-47`），与运行时无关。

**有没有 step 级重试/局部重规划**：没有。`_call` 只做一次调用，异常统一包成 `OrchestrationError`（`409-413`）；`_call_json` 解析失败直接抛（`415-418`）。唯一的"回到上一步"是 Auditor 打回后重跑一次 Worker（`252-274`），且这次重跑不受预算约束（预算在生产未接线，见 §0）。`auditor` 判 fatal → 状态机 `audit_fatal` → 抛错（`243-245`）；修订失败 → 保留草稿、连推 `repair_done`+`audit_pass` 降级交付（`264-267`）。

**一处未收敛路径**：`outcome == "repair_required"` 但 `issues` 为空时不推进状态机（`246-249` 的两个分支都不命中），直接返回草稿（`276-277`）——任务会停在 `AUDITING`，既不 COMPLETED 也不 FAILED。`verdict` 认不出时也会被归一成 `repair_required`（`232-235`），所以这条路径现实可达。

**Auditor 引用校验是可选强度**：只有 `audit.refs` 非空才校验（`237-242`，注释自称"没给则保留宽松模式"）；`known_ids` 不可调用时返回 None 直接放行（`371-379`、`policies.py:208-209`）。

**调用图**（谁调谁）：

```
POST /tasks/{id}/orchestrate          atlas/api/routes.py:147-208
  └─ runtime.get_state                runtime.py:193       （顺带 _load→resume→rebuild_engine）
  └─ RepositoryGraph                  atlas/api/graph.py:15-54
       ├─ state_view → 5 行摘要       graph.py:24-28        ← 不是四区
       └─ role_note → ServiceRuntime.note → repository.add_node
  └─ Orchestrator(llm, graph, research_tool)   routes.py:166  ← 无 machine / 无 budget / 无 llm_no_think
       ├─ _call/_call_json → 注入的 llm        orchestrator.py:409-418
       ├─ graph.state_view ×3                  orchestrator.py:180,206,221
       └─ _research → ResearchTool             orchestrator.py:281-359
  └─ 事后手动推进状态机 3 次                routes.py:187-199
```

关键结论：**API 路径下 `_transition` 全是 no-op**（`orchestrator.py:163-171` 在 `machine is None` 时 return），状态推进由 `routes.py:187-199` 事后补，且只写死 `plan_ready/work_done/audit_pass` 三个触发词 → **`REPAIRING` 状态在最外层 API 上不可达**，`audit_repair/repair_done` 从未经过 HTTP 路径。另两条路径都注入 machine：离线集成 `integration/merged_e2e.py:219-225`、评测编排臂 `eval/atlas_agent.py:119-127`（每子任务一个新实例，跑完即丢）。

## 8. 工具层：失败分类 / trace / provenance

**有哪些工具**：不在 `atlas/tools/` 里实现检索，只定义协议 + 三个适配器：`LocalResearchTool`（进程内函数，`compassrag.py:25-51`）、`HttpCompassRAGTool`（POST `/research`，`58-116`）、`CompassRAGAgentTool`（进程内桥接真实 Agent，`143-214`）。协议面是 `ResearchRequest/ResearchResult/EvidenceItem/ResearchToolError`（`tools/protocol.py:43-180`）。

**失败分类**：五类 `timeout/invalid_request/no_evidence/budget_exhausted/upstream_error`（`protocol.py:13-20`），`ResearchToolError` 构造时校验 kind 合法（`34-40`）。HTTP 状态码映射：400/422→invalid_request、408/504→timeout、429→budget_exhausted、404→no_evidence、其余→upstream_error（`compassrag.py:119-128`）；CompassRAG 侧失败词表再映射一次，`cancelled` 被归到 `upstream_error`（`132-140`）。

**有没有超时/重试/幂等键**：有超时和幂等键、**没有重试**。`HttpCompassRAGTool(timeout_s=30.0)` 传给 `urlopen`（`compassrag.py:61-70,88`）；幂等键放在请求体与 `X-Idempotency-Key` 头（`76-86`），`X-Request-Id` 放 trace_id（`82`）。适配器内部不做重试（`research` 只调一次）；`research_max_rounds=2` 是传给上游的"检索轮次"参数（`orchestrator.py:133,294`），不是失败重试。上层拿到 `ResearchToolError` 后的处理是：写 `trace.research_error`、不写图、不改状态（`orchestrator.py:302-308` + `281-285` 的 docstring），然后继续用原问题跑 Worker。**能证明**：工具失败被隔离、可分类、可审计。**不能证明**"失败后会自动重试或降级换工具"——没有重试循环，也没有备用工具链。

**trace 与 provenance 的写入点**：
- 结构化 trace：`Tracer.record()` 13 个字段（`tracing.py:19-33,83-125`），落库靠 `sink`；`AgentRuntime._tracer` 把 sink 接到 `repository.record_trace`（`runtime.py:322-328`），后者写一条 `operation="trace"` 的事件（`repository.py:442-445`）。HTTP 层另有**手写** trace：research 成功/失败各一条（`routes.py:103-108`、`132-137`），编排失败一条（`174-178`）。**注意**：编排成功时**不写 trace**——Planner/Worker/Auditor 的调用、token、延迟都不落库，只有最终 answer 作为 Artifact 节点进图。
- provenance：不是写入，是**只读治理审计**。`audit_nodes` 扫描活跃节点，产出四类问题：过期、未验证 Artifact、缺 source、同 key 约束冲突（`provenance.py:131-161`）；`PROVENANCE_FIELDS` 六个字段是"应该能说清"的清单（`18-25`），`risk_lines` 把问题渲染成计划区末尾的风险段（`164-169`）。没有任何代码因缺 source 而拒绝写入；`missing_provenance` 只在报告里出现。
- 数据来源：`source/confidence` 由调用方传（`service.py:247-278`），研究 Fact 的 `meta` 里带 `research_trace_id/rounds/confidence/stop_reason`（`orchestrator.py:346-353`、`routes.py:125-130`），报告据此从 Fact 下钻到 CompassRAG 单次轨迹（`report.py:52-68,131-149`）。

## 9. 评测判分逻辑与过宽点

**两套评测，口径不同**。

A. 离线 harness（`atlas/evaluation/`）：自建 fixture（`fixtures.py:63-211` 手写三类家族 + 12 步长任务），三种记忆策略对照（`policies.py:52-142`），研究者可以是脚本替身或真实模型（`harness.py:135-228`）。判分：
- 归一化：去 LaTeX、去空格、转小写（`harness.py:96-106`）。
- 完成判定：`contains_probe(answer, probe)` 即 `probe` 归一化后是答案的**子串**（`109-114`）；真实模型还要求"答案提到问题主体"（`117-132`），主体候选是英文词/数字/中文二字滑窗（`121-131`）。
- 违规：答案里出现 `forbidden_method` 子串（`220-222`）；过期误用：出现 `stale_value` 且不出现目标（`223-226`）。
- 聚合：完成率按整条任务（`metrics.py:36`）；违规率/误用率"任一步发生即 1"（`37-38`）；`recovery_success_rate` 在没有中断样本时**记 1.0**（`39-41`，docstring 自陈"不允许没有样本却宣称 100%"，但实现仍是 1.0，只是额外报了 `interrupted_runs`）。

B. 端到端脚手架（`integration/merged_e2e.py`）：4 个场景 + 1 个恢复演练，Atlas 角色与 CompassRAG 的 LLM 全是脚本替身，检索器是固定 ranking 的关键词替身（`52-59`、`62-85`），语料是手写 5 条（`38-49`）。

**具体宽松点**（每条都能直接引）：

1. **子串即正确**：`probe` 常是 `"4γ"`、`"1.75"`、`"引理 2"`（`fixtures.py:91,99,106,198`），只要答案包含该子串就得 1 分，答非所问也算对（`harness.py:109-114`）。长任务 12 步的 probe 就是纯数字（`fixtures.py:198`）。
2. **主体校验很弱**：只要命中任意一个候选词，而不是判断主体与答案的语义对应（`harness.py:132`）。
3. **延迟是编造值**：`latency_ms = context_tokens*0.5 + model_calls*8` 直接合成（`harness.py:422`），`latency_source="synthetic_estimate"`（`63`）；成本也是 `context_tokens/1000*固定价`（`423`）。
4. **恢复成功率无样本记满分**（`metrics.py:39-41`）。
5. **`coverage_ok` 自证**：期望 chunk 集合来自场景自己写的 `ranking`（`merged_e2e.py:118-121,254`），检索器按同一 ranking 返回（`59`），所以覆盖一定成立。
6. **`revision_ok` 只看形态**：`bool(superseded) and bool(active_artifacts)`（`259`），不校验修订稿内容是否真的修正了问题。
7. **`conflict_ok` 只看来源数**：`len(evidence_sources) >= 2`（`260`），不判断冲突是否被正确暴露。
8. **`tool_failure_ok` 排除了 timeout**：只接受 `no_evidence/budget_exhausted`（`261-265`），而超时是更常见的失败；且该场景照样传了工具（`224` 的三元表达式两个分支相同 `tool if expect_research else tool`，等于没写条件）。
9. **"完成"可绕过研究失败**：`completed` 只要求状态机 COMPLETED 且 answer 非空（`247`），`tool_failure` 场景审计判 ok 就 Completed，研究失败不影响完成率。
10. **`duplicate_facts` 口径**：`max(0, research_fact_count-1)`（`330`），不检测内容重复、不检测跨场景重复。

**fixture 是自建的**：是，手写语料 + 手写 expected chunks（`merged_e2e.py:38-49,114-155`），文本里明确写着"离线脚手架…不是效果数字"（`339-340`），测试也断言这句声明存在（`tests/test_merged_e2e.py:13`）。**能证明**：流水线闭合、判分机制可跑、恢复可用。**不能证明**：任何效果数字或可外推的能力差异。

**指标怎么算**：`aggregate` 固定 8+3 项（`metrics.py:14-23`、`30-46`），分母是 run 数（不是步数），所以 2 步任务和 12 步任务权重相同。`failing_runs` 只列"未完成/违规/误用/报错"的步（`102-121`）。

## 10. 未接入点与最小改造清单（按 RQ 分类）

**两条互不相交的路径**（这是全篇最重要的事实）：

| | 任务运行时路径 | MemoryArena 记忆路径 |
|---|---|---|
| 入口 | `atlas/api/routes.py:28` | `eval/atlas_memory_server.py:192` |
| 状态 | repository + checkpoint + 状态机 | 无（进程内 `pending_subtask`，`service.py:71`） |
| 图 | `nodes/edges` 表，每次写后全量 `rebuild_engine`（`service.py:279`） | 内存 `GraphMemoryEngine` + JSONL dump（`service.py:200-208`） |
| 上下文 | `RepositoryGraph.state_view` 5 行摘要（`api/graph.py:24-28`） | 四区 `ContextAssembler`（`service.py:189-198`） |
| 事件/replay | 有 | 无（镜像到库是旁路，`atlas_memory_server.py:170-182`） |
| 编排 | `/orchestrate` 固定三段式 | `eval/atlas_agent.py` 固定三段式 |

**未接入点清单**（每条都是"能力存在但没被这条路径调用"）：

1. 四区组装器：`atlas/runtime.py`、`atlas/orchestration/`、`atlas/api/` 零引用（§6 调用方清单）。→ 角色拿到的是 `- fact: 前 60 字` 的 5 条摘要（`api/graph.py:27`），活跃约束/决定**靠不住**；`state_view` 也不做查账。
2. 状态机在 `/orchestrate`：未注入 machine（`routes.py:166`）→ 无非法转移保护、无修订预算、无 `REPAIRING`。反例：`integration/merged_e2e.py:224`、`eval/atlas_agent.py:125` 都注入了。
3. 统一预算：`Budget` 只在测试实例化（`tests/test_state_machine.py:442-481`）；生产 `budget=None` → `_charge` no-op（`orchestrator.py:361-369`）。
4. 重试/超时策略：`call_with_policy`/`RetryPolicy`/`ROLE_POLICIES` 只在 `tests/test_state_machine.py:202-247` 被调用；`orchestrator._call` 无超时（`409-413`）。结果是"角色调用挂住就整条任务挂住"。
5. LangGraph 控制流：`build_control_flow_graph` 只有测试调用（§1）→ 简历/文档里"LangGraph 编排"在运行时路径上**未找到证据**。
6. 失效语义：`expire()` 无生产调用者（§5）；`supersede` 仅 Artifact 修订一处。→ 没有"Fact 被推翻"的运行时路径。
7. provenance 治理：只在 `get_state` 与组装期读取（`runtime.py:197`、`assembler.py:145`），不阻断写、不影响重试。
8. 编排 trace：成功路径不写 trace（§8）→ 无法从 trace 表复原一次编排的 token/延迟/重试。
9. 镜像保真：`import_engine` 跳过已存在节点（`repository.py:469-471`）→ 后发生的生命周期变更不会进事件表（§3）。
10. checkpoint 语义：`retry_count` 恒 0、`graph_version` 是节点数（§2、§1）→ 恢复信息不足以续跑"正在进行的工具调用"。

**最小改造清单**：

- **RQ1（状态/恢复可信）**：① 给 `Orchestrator` 注入 `machine`（改 `routes.py:166` 一行）；② 把 `audit_repair/repair_done` 纳入 API 事后推进序列（`routes.py:188-190`）；③ 修 `repair_required` 且 `issues` 为空时不收敛的问题（`orchestrator.py:246-249`）；④ checkpoint 增加"当前步骤 + pending 工具调用"字段（`models.py:121-138` + `runtime.py:124`）。
- **RQ2（上下文一致性）**：⑤ 让 `RepositoryGraph.state_view` 改调 `ContextAssembler`（`api/graph.py:24-28`），这样四区与查账才覆盖编排路径；⑥ 把 `Budget` 接入 `Orchestrator`（`routes.py:166` 传 budget，`orchestrator.py:134` 已有参数）。
- **RQ3（工具可靠性）**：⑦ 把 `call_with_policy` 接进 `_call`（`orchestrator.py:409-413`），至少给 Planner/Auditor 2 次、Worker 3 次（策略已在 `policies.py:119-123` 写好）；⑧ 对 `timeout/upstream_error` 做一次重试、`no_evidence` 不重试（分类已就绪 `protocol.py:13-20`）；⑨ 编排成功也写 trace（`routes.py:168-171` 之后）。
- **RQ4（评测有效性）**：⑩ probe 改成结构化断言或至少同义短语集合（`harness.py:109-114`），并要求答案不含冲突值；⑪ `metrics.recovery_success_rate` 无样本时置 `None` 而非 1.0（`metrics.py:39-41`）；⑫ `merged_e2e` 的 `revision_ok` 校验修订稿文本、`coverage_ok` 用独立金标、修掉 `tool if expect_research else tool`（`224,254,259`）；⑬ 延迟要么实测要么从表里删掉（`harness.py:422`）。

---

**审计边界声明**：本文件只覆盖 `atlas/`、`integration/`、`eval/` 三个目录的 Python 代码与 `tests/` 中相关用例；未审计 `compassrag/` 内部实现（Atlas 只经 `ResearchTool` 协议调用它）、未审计 `web/` 前端、未改动任何代码。第 10 节的改造清单是"接线位置"建议，不是已完成的工作。
