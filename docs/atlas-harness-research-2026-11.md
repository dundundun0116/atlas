# Atlas 长程 Agent Harness 调研报告

> **范围**：RQ1 长程规划与任务分解 / RQ2 长期记忆与上下文管理 / RQ3 环境状态感知与变化适应 / RQ4 可靠性、恢复与长程评测
> **时间**：2026-11-04　**证据截止**：本报告所有论文元数据与仓库代码路径均在本次会话内实取核验
> **读者**：Atlas 项目本人（研究者视角），用于架构定稿与对外表达
> **配套材料**：`notes/research-2026-11/` 下四份分报告（rq1–rq4）、`atlas-audit.md`（代码取证）、`venue-verified.md`（元数据核验表）

---

## 0. 结论摘要

**一句话**：Atlas 的方向是对的（状态化 Harness + RAG 降级为 Tool），文献能给每条设计提供支撑；但当前最大的问题不是"缺机制"，而是**机制没有接线**——4 处死代码、两条路径不汇合、6 个文档承诺与代码现实之间的缺口。这份调研的主要价值，是把这 6 个缺口各自对上一个**已被验证过的机制**。

**十条硬结论**：

1. **Atlas 的机制骨架基本都在，但每条只在一条路径上生效**：状态机+checkpoint 只在 `/tasks/*`，四区上下文只在 MemoryArena memory-server，两者从未汇合。`/orchestrate` 拿到的是 **5 行摘要**（`api/graph.py:24-28`），不是四区。

2. **四处"有实现、无生产调用"的死代码**：LangGraph 控制流、`Budget`/`RetryPolicy`、`expire()`、`checkpoint_sink`。任何"我们实现了 X"的说法都必须附一条生产路径调用栈。

3. **RQ1：17 个工作里只有 5 个真正持有"计划数据结构"**（LATS 的树、LLMCompiler 的 DAG、HuggingGPT 的 dep 数组、ToT 的 beam、Magentic-One 的文本账本）。**"子目标级局部重规划"出现次数为 0**——所有系统的重规划只有"整段重来"与"整计划重写"两种粒度。这是 Atlas 最大、也最可辩护的空白位。

4. **RQ1 的方法论教训**：论文里的"树"≠实现里的"树"（ToT 的 DFS 根本没开源）；LangGraph 不是 DAG 执行器而是 BSP 超步引擎。**不做三栏区分，就会把别人的宣传当成自己的设计依据。**

5. **RQ2：六类记忆范式里，只有"结构化状态记忆"能原生表达失效与冲突**；其余五类本质是 append-only 加一次再检索。**图记忆与状态记忆正交**——HippoRAG 的 PPR 只是索引，`delete` 是删数据不是标记失效；A-MEM 的 evolution 更新的是表示不是真值。

6. **RQ2 最硬的一条证据来自 Mem0 自己的代码**：同一份代码里 `DEFAULT_UPDATE_MEMORY_PROMPT` 声明四操作、写明"矛盾即删"，但默认路径 `main.py:944` 走的是 `"sole operation is ADD"`。**工业向量记忆在规模化压力下主动放弃了状态语义**——这比任何论证都能说明"把记忆当检索管"会退化到什么地步。

7. **RQ2 的纵向证据**：Ground Truth First（arXiv 2607.21962）显示记忆架构排名会随历史长度**反转**（curated-map 3 周 96% → 9 周 72%，provenance-typed graph 反升 90%，p=0.031），且**写入质量决定下游**（弱写入失败率 24% vs 强写入 2%）。→ Atlas 的 `provenance` 门禁必须从"组装时过滤"前移到"入图前拒绝"。

8. **RQ3：没有一个调研系统在 agent 侧维护环境状态模型**。光谱分布：agent 侧 a 档为空、纯重观测 11/18、唯一每步验证的是 BrowserGym（且验证前后备份恢复 page/history）、声称有状态但没落盘的是 OpenHands。**"动作已提交但结果未知"全行业零建模**——10 个系统里没有一个能表达"不知道"。**Atlas 要占的正是第四格。**

9. **RQ3 的现成解法在 agent 社区之外**：Temporal 的三种超时（Schedule-To-Start / Start-To-Close / Schedule-To-Close）+ Heartbeat、Stripe 的"执行即存结果"、saga 的补偿必须幂等、IETF 的幂等键 fingerprint/TTL、fencing token 让下游拒绝陈旧写。这些直接构成 `PendingToolCall` 状态机与对账协议。

10. **RQ4：可靠性机制里只有 verifier 与一致性度量有硬论文证据**；checkpoint/retry/幂等/补偿/budget/exactly-once 在 Agent 领域**没有同行评审对照实验**，只能写进 Implementation 而非 Contributions。`exactly-once` 不要碰。

**三条必须立刻纠正的表述问题**：

- **40/40 没有统计意义**：Wilson 95% CI 下限 0.912；独立两臂检测 60% vs 70% 每组约需 356 个任务。5 个自建场景只能做机制演示。
- **延迟与成本是合成值**（`harness.py:422` 的 `latency_source="synthetic_estimate"`），不能进任何效果表。
- **"self-correction"要改名**：ICLR 2024《LLMs Cannot Self-Correct Reasoning Yet》是强反例；Atlas 的做法应表述为 "bounded repair with external verifier"。

**一个必须先内部对齐的口径冲突**：`PROJECT.md` v0.3.1 已声明**旧 40 步真实模型数字撤回**（fixture 预置未来事实 + probe 过宽），但 `LEDGER.md` 仍把它列为"主力证据（未变）"。**对外材料在重跑完成前不得引用 244,381 / 62,680 这组数**（详见 §10）。

---

## 1. 方法与证据口径

### 1.1 检索渠道与实测可用性

这份报告在**网络严重受限**的环境下完成，渠道可用性直接决定了哪些结论能给出、哪些只能标"待核实"。实测结果如下（2026-11-04）：

| 渠道 | 状态 | 用途 | 后果 |
|---|---|---|---|
| arXiv abs 页（curl + `Comments:` 字段） | 可用 | 标题/作者/年份/**作者自填的会议信息** | 主力渠道 |
| OpenReview API v2 `notes/search` → `content.venue.value` | 可用 | NeurIPS / ICLR / TMLR / COLM 的**权威 venue 标签**（如 `NeurIPS 2023 oral`、`ICLR 2024 poster`） | 交叉验证主力 |
| PMLR 卷 TOC（grep） | 可用 | ICML / CoRL / AISTATS 录用证据 | 补齐 LATS、SayCan |
| Crossref `?mailto=` | 可用（无 mailto 会 429） | AAAI / ACM 等有 DOI 的会议 | 补齐 MemoryBank 等 |
| ACL Anthology | 可用 | ACL / EMNLP / NAACL | 补齐 Plan-and-Solve 等 |
| GitHub REST API | 可用 | 仓库实现证据（文件路径、代码） | "工程真正实现了什么"的唯一硬证据 |
| **api.openalex.org** | **不可用**（429，免费预算耗尽，约 8h 恢复） | 原本是 venue/引用数主力 | 引用数一律不给 |
| **api.semanticscholar.org** | **不可用**（429） | 同上 | 引用图谱分析放弃 |
| **dblp.org** | **不可用**（Anubis 反爬，HTTP 202） | CS venue 权威索引 | — |
| **proceedings.neurips.cc / papers.nips.cc** | **不可达**（code 000） | NeurIPS 官方页 | 改用 OpenReview venue |

**这带来三个必须写进结论的局限**：

1. **引用数一律未提供**。任何"被引 N 次"的说法都无法在本环境核验，本报告不写。
2. **少数 venue 标为待核实**。目前只剩 `Generative Agents`（通行说法 UIST 2023，本轮未取到 ACM DL/Crossref 证据）。其余 35 个工作均有实取渠道。
3. **没有做系统性文献检索（PRISMA 式）**。这是**定向机制调研**，不是 meta-analysis：纳入标准是"与四个 RQ 的机制直接相关"，不是"穷尽该主题所有论文"。

### 1.2 一个反复踩到的坑：三种"venue"口径都能骗人

写这类报告最容易犯的错，是把索引层的字段当成事实。本报告统一了核验规则，并记录三个反例：

| 陷阱 | 实例 | 正确处理 |
|---|---|---|
| `paper_cite` / DOI 注册库返回的 venue 是 **arXiv 版本** | ReAct 被返回为 "arXiv"，实际是 ICLR 2023 | 只用它核**标题/作者/年份/ID**，不用它核会议 |
| `paper_search` 结果的 venue 列只显示 **arXiv** | ToT 显示 "2023 · arXiv"，实际 NeurIPS 2023 oral | 索引口径 ≠ 发表口径 |
| OpenReview 的 **"Submitted to X" 是投稿记录，不是录用** | LATS 显示 "Submitted to ICLR 2024"，实际 **ICML 2024**；AutoGen 显示 "Submitted to ICLR 2024"，实际 **COLM 2024** | 只认 `venue` 为 `XXX YYYY [oral/poster]` 或 `Accepted by TMLR` 形态的标签 |

### 1.3 证据分级（本报告统一使用）

| 级别 | 定义 | 本报告中的典型例子 |
|---|---|---|
| **A（高）** | 读了原文/源码，且元数据经上述渠道实取核验；机制描述可指到具体文件或小节 | SWE-agent 的 ACI 与主循环（源码）；Atlas 的 state_machine（源码） |
| **B（中）** | 元数据已核验，机制来自摘要/官方文档/README，未逐行读实现 | 多数 benchmark 论文的判分协议 |
| **C（低）** | 仅二手来源（博客、二手综述、他人转述），或实现与宣称不一致且未查清 | 部分"生产级"记忆系统的性能宣称 |

### 1.4 三栏区分法（贯穿全文）

对每个工作，本报告尽量区分：

- **① 论文提出的机制**：论文正文写了什么、实验证明了什么；
- **② 工程真正实现的机制**：仓库代码里能跑的东西（以 GitHub API 取到的文件路径为证）；
- **③ 仅宣传/概念层的机制**：README、博客、幻灯片里有，代码里查不到。

这个区分不是学术洁癖。Atlas 的核心风险恰好是"**机制存在但没接线**"——代码审计已经证明 Atlas 自己有 4 处这样的死代码（§2.3）。如果调研不区分这三栏，就会把别人的宣传当成自己的设计依据。

---

## 2. 现状基线：Atlas 现在到底有什么（代码取证）

> 本节全部结论来自只读审计 `notes/research-2026-11/atlas-audit.md`，每条带 `文件:行号`。这是后面所有"可借鉴/不可照搬"判断的地基——**不先搞清自己有什么，就无法判断该借什么**。

### 2.1 一句话结论

**Atlas 的机制骨架基本都在代码里（7 状态机、只追加事件、四区组装、图记忆失效语义、五类工具错误分类），但每条机制都只在某一条路径上接线，而且有四处"有实现、无生产调用"的死代码。** 最要命的是两条路径从未汇合：

| | 任务运行时路径 | MemoryArena 记忆路径 |
|---|---|---|
| 入口 | `atlas/api/routes.py:28` | `eval/atlas_memory_server.py:192` |
| 状态 | repository + checkpoint + 状态机 | 无（进程内 `pending_subtask`，`atlas/service.py:71`） |
| 上下文 | `RepositoryGraph.state_view` 的 **5 行摘要**（`atlas/api/graph.py:24-28`） | 四区 `ContextAssembler`（`atlas/service.py:189-198`） |
| 事件/replay | 有 | 无（镜像到库是旁路） |
| 编排 | `/orchestrate` 固定三段式 | `eval/atlas_agent.py` 固定三段式 |

也就是说：**Atlas 对外演示的四区上下文，和 Atlas 实际跑任务时用的上下文，不是同一个东西。**

### 2.2 逐项现状（21 项能力速览）

| 能力 | 有无 | 证据 | 覆盖范围 | 关键差距 |
|---|---|---|---|---|
| 7 状态机 + 合法转移表 | 有 | `orchestration/state_machine.py:23-55` | 全路径 | 状态只在 checkpoint JSON 里，无独立表 |
| 非法转移拒绝 + 留痕 | 有 | `state_machine.py:158-182` | 有 machine 注入的路径 | `/orchestrate` 未注入 machine，`_transition` 全 no-op（`orchestrator.py:163-171`、`routes.py:166`） |
| checkpoint | 有（三处写） | `runtime.py:124,153,180` | 仅 advance/pause/resume | 编排内部步、节点写入、工具调用都不写 |
| pause/resume | 有 | `runtime.py:164-189` | `/tasks/*` | 恢复只回状态机，**不回上下文**（无 pending 工具调用/计划快照） |
| 只追加事件 | 代码纪律级 | `storage/events.py:12-18` | 全路径 | 无 DB 级不可变约束；`seq` 用 `max+1`（`repository.py:622-626`），并发可重复 |
| replay | 部分 | `events.py:41-74` | 只重建节点生命周期状态 | 图本体从 `nodes` 表重建，**不是事件溯源** |
| 幂等（请求级） | 有 | `runtime.py:330-348` | create/advance/pause/resume | `/orchestrate`、`/research` 的图写入不走这套 |
| 幂等（节点级） | 有 | `repository.py:189-197` | 调用方传 key 时 | 编排 `role_note` 没有该参数（`api/graph.py:30-54`） |
| 图记忆 schema | 有 | `memory/schema.py:14-65` | 内存引擎 + repository 双实现 | 两套实现无共享校验层 |
| 失效/被替代语义 | 有 | `schema.py:34-38`、`repository.py:273-322` | 引擎/仓储层可用 | 生产仅"修订稿作废草稿"一处；`expire()` **无调用者** |
| 四区上下文 | 有 | `context/assembler.py:26-32,70-118` | 仅 memory-server 与离线评测 | runtime / orchestration / api **零引用** |
| 结转 carryover | 有 | `context/carryover.py:16-45` | 仅 memory-server 路径 | 与 checkpoint 无关，是进程内状态 |
| 固定三段式编排 | 有 | `orchestrator.py:173-277` | API / eval / 集成三条路径 | 无 DAG、无 step、无依赖调度 |
| 统一预算 Budget | **仅有类** | `orchestration/policies.py:59-115` | 只在测试里实例化 | 生产 `_charge` 恒为 no-op（`orchestrator.py:361-369`） |
| 角色重试/超时策略 | **仅有类** | `policies.py:32-38,119-167` | 只在测试调用 | `_call` 无重试无超时（`orchestrator.py:409-413`） |
| LangGraph 控制流 | **有实现、无生产调用** | `state_machine.py:268-301` | 仅 `tests/test_state_machine.py:328-360` | 不挂任何 runtime/API 路径 |
| 工具失败五分类 | 有 | `tools/protocol.py:13-20` | 全部 ResearchTool 调用 | 分类后上层只记 trace，不重试不降级 |
| trace | 有（两套） | `observability/tracing.py`、`repository.py:442-454` | runtime 走 sink；routes 手写 | **编排成功时完全不写 trace** |
| provenance | 有（只读审计） | `context/provenance.py:18-70` | 组装期与读取时 | 不写库、不阻断写入 |
| 评测判分 | 有（过宽） | `evaluation/harness.py:96-132`、`integration/merged_e2e.py:244-266` | 离线自建 fixture | 见 §2.4 |
| Step/Dependency/Scheduler/PendingToolCall | **无** | `grep` 在 `atlas/` 无命中 | — | 这正是 RQ1/RQ3 的核心缺口 |

### 2.3 四处死代码与三处语义错位（最容易被外部评审击穿的地方）

**死代码**（全部经全仓 grep 验证，生产路径零调用）：

1. `build_control_flow_graph`（LangGraph 控制流）—— 只在 `tests/test_state_machine.py:328-360`。
2. `Budget` / `call_with_policy` / `RetryPolicy` —— 只在 `tests/test_state_machine.py` 实例化；生产 `budget=None`。
3. `ServiceRuntime.expire` —— 无任何调用者。
4. `StateMachine.checkpoint_sink` —— 只在 `tests/test_research_tool.py:312` 接线。

**语义错位**：

- `CheckpointRow.graph_version` 写的是**节点个数**（`service.py:316`），不是 `SessionRow.graph_version` 那个版本计数器（`repository.py:656-660`）——两者不是同一个量。
- `CheckpointRow.retry_count` **恒为 0**（`service.py:303` 默认值，三处调用都不传）。
- replay 只重建"节点生命周期状态"，`link` 事件完全不参与（`events.py:45` 注释明说）——**仅凭事件表无法还原一张图**。

**这三处合起来意味着**：Atlas 目前不能声称"事件溯源架构"，只能声称"事件只追加 + 状态快照 + 有限 replay"。这是一个**措辞必须收紧**的地方。

### 2.4 评测判分的 10 个过宽点（自曝清单）

离线 harness 与 `integration/merged_e2e.py` 的判分存在系统性宽松，逐条可引：

1. **子串即正确**：`probe` 常是 `"4γ"`、`"1.75"`（`fixtures.py:91,99,106`），答案包含子串即得 1 分（`harness.py:109-114`）。
2. **主体校验弱**：命中任意候选词即可，不判断语义对应（`harness.py:132`）。
3. **延迟是合成值**：`latency_ms = context_tokens*0.5 + model_calls*8`（`harness.py:422`），`latency_source="synthetic_estimate"`。
4. **恢复成功率无样本记 1.0**（`metrics.py:39-41`）。
5. **`coverage_ok` 自证**：期望 chunk 来自场景自己写的 `ranking`，检索器按同一 ranking 返回（`merged_e2e.py:118-121,254`）。
6. **`revision_ok` 只看形态**：`bool(superseded) and bool(active_artifacts)`（`:259`），不校验修订内容。
7. **`conflict_ok` 只看来源数**：`len(evidence_sources) >= 2`（`:260`）。
8. **`tool_failure_ok` 排除 timeout**：只接受 `no_evidence/budget_exhausted`（`:261-265`），而超时更常见；且 `:224` 的 `tool if expect_research else tool` 两分支相同。
9. **"完成"可绕过研究失败**：`completed` 只要求状态机 COMPLETED 且 answer 非空（`:247`）。
10. **`duplicate_facts` 口径**：`max(0, count-1)`（`:330`），不检测内容重复。

**结论**：现有 e2e 能证明"流水线闭合、判分机制可跑、恢复可用"，**不能证明任何效果数字**。这一点必须与"40 步真实模型评测"的两层口径严格分开（见 §7.4）。

### 2.5 目标架构文档与代码的实际距离

`docs/atlas-harness-architecture.md` 已经写出了正确的目标口径（单一控制中心、Execution State / Knowledge State 二分、Scheduler + Verifier + Pending Tool Call、RAG 降级为 Tool）。**问题不在文档，而在文档与代码之间的 6 个缺口**：

| 文档承诺 | 代码现实 | 缺口性质 |
|---|---|---|
| Scheduler 按依赖选下一步 | Planner 输出只有 `{route, risks, reason}` 三个字符串（`orchestrator.py:29-42`），**没有 step 列表、没有依赖字段、没有 step id** | 数据结构缺失 |
| Execution State 含 Pending Tool Call | 全仓无 `PendingToolCall`；checkpoint 只有 `task_state/graph_version/pending_role/retry_count/context_digest_version` | 模型缺失 |
| Context Engine 从状态组装上下文 | 编排路径拿到的是 5 行摘要（`api/graph.py:24-28`），四区组装器零引用 | **接线缺失** |
| 统一管理预算、重试 | `Budget`/`RetryPolicy` 是死代码 | 接线缺失 |
| Verifier 检查产出与证据 | Auditor 存在，但引用校验只在 `audit.refs` 非空时生效（`orchestrator.py:237-242`），且 `known_ids` 不可调用时直接放行 | 强度不足 |
| 暂停/恢复后继续执行 | resume 只重建状态机与图，**不恢复上下文与在途工具调用** | 语义缺失 |

这 6 条恰好一一对应 RQ1–RQ4。**所以这份调研的真正任务不是"再找一些论文"，而是为这 6 个缺口各找一个已被验证过的机制。**

---

（RQ1–RQ4 分节与最终建议见后续章节）
## 3. RQ1：长程规划与任务分解

> 完整 17 个工作模板见 [`notes/research-2026-11/rq1-planning.md`](../notes/research-2026-11/rq1-planning.md)。

### 3.1 五档机制谱系

| 档 | 计划载体 | 代表 | 计划可修改性 | 状态可恢复性 |
|---|---|---|---|---|
| **L0 内隐式** | 无（提示里的 Thought 串） | ReAct、SWE-agent、OpenHands | 不可修改（只能继续生成） | 仅靠轨迹日志 |
| **L1 显式线性** | 有序子目标列表 | Voyager `sub_goals`、Plan-and-Solve | 可跳过，不可重排 | 弱（进度＝已完成计数） |
| **L2 依赖图** | DAG（任务＋依赖边） | LLMCompiler、HuggingGPT、ADaPT | 可整图重写，不可局部改边 | 中（LLMCompiler 全内存） |
| **L3 树搜索** | 搜索树 / beam | ToT（beam）、LATS（MCTS） | 可回溯、可剪枝 | 无（LATS 全内存） |
| **L4 分层＋账本** | 目标→子目标→动作＋进度账本 | Magentic-One（Task/Progress Ledger）、SayCan | 整账本重写 | 中（AutoGen 无 step 持久化） |

### 3.2 四条硬结论（比"某论文提出了什么"更重要）

1. **17 个工作里，运行时真正持有"计划数据结构"的只有 5 个**：LATS 的 MCTS `Node` 树、LLMCompiler 的 `Task.dependencies` 整数 DAG、HuggingGPT 的 `command["dep"]` 数组、ToT 的 beam 列表、Magentic-One 的文本账本。**其余 12 个的"计划"要么是提示词里的一段文字，要么根本不存在。**

2. **"子目标级局部重规划"在这 17 个工作里出现次数为 0。** 可观测的重规划粒度只有两种：**整段重来**（Reflexion 的 `while trial_idx < num_trials`、SWE-agent 的 `ScoreRetryLoop`）与**整计划重写**（LLMCompiler 的 `for i in range(max_replans)`、Magentic-One 的 `_update_task_ledger()`）。**没人做"只改坏掉的那个子目标及其依赖"**——这正是 Atlas 的真空位，也是最值得写成贡献的一条。

3. **论文里的"树"不等于实现里的"树"。** ToT 论文给了 BFS/DFS，开源实现 `src/tot/methods/` 下**只有 `bfs.py`**、没有 `dfs.py`；且 `solve()` 里的 `ys` 只是一串候选字符串，**没有 parent 指针、没有回溯、没有节点级状态**。ToT 只能作为"搜索思想"引用，不能作为"实现参照"。

4. **step 级持久化只有 LangGraph 做了，而它持久化的不是"计划"而是"通道版本"。** `_loop.py::tick()` 每超步写 checkpoint，`_algo.py::apply_writes()` 里 `channel_versions` / `versions_seen` 才是恢复依据；计划节点本身没有"已完成/失败"状态字段。**且 LangGraph 不是 DAG 执行器，是 BSP 超步引擎**——`_triggers()` 的触发条件是"该节点读的任一 channel 版本 > 上次见到的版本"，不是"所有前驱已完成"，支持环（条件边回边）。把它当"DAG 编排"宣传是常见误解。

### 3.3 三栏对照：论文机制 / 工程实现 / 仅宣传

| 工作 | ① 论文提出 | ② 工程真正实现（文件:符号） | ③ 仅宣传/概念层 |
|---|---|---|---|
| ReAct | Thought-Action-Observation 交替 | `hotpotqa.ipynb::webthink()` 固定 7 轮＋prompt 拼接 | "可控推理"实为少量 few-shot 模板 |
| ToT | 树搜索（BFS＋DFS） | 仅 `src/tot/methods/bfs.py`（beam，无节点对象） | **DFS 未开源**；"回溯"仅论文层 |
| LATS | MCTS＋反思 | `hotpot/lats.py::Node/select_node/lats_search` 完整树＋剪枝＋回传 | "统一推理-行动-规划"的话语层 |
| Reflexion | 语言化强化学习 | `alfworld_runs/main.py` 整局循环＋`generate_reflections.py::update_memory` | "无需梯度即自我改进"的强表述 |
| Voyager | 自动课程＋技能库＋迭代提示 | `voyager.py::inference`（扁平 sub_goals）、`skill.py`、`curriculum.py::update_exploration_progress`（**失败即跳过**） | "终身学习"实为单次长跑内的技能累积 |
| SWE-agent | ACI 决定性能 | `sweagent/agent/agents.py::DefaultAgent.run` **线性循环**＋`reviewer.py` 整轮重试 | "自主修复"＝reviewer 择优 |
| OpenHands | 通用开发平台 | 事件流＋`stuck_detector.py` 四类模式＋`goal/controller.py` | **"规划"＝提示工作流＋`.agents_tmp/PLAN.md`，无机器可读计划** |
| AutoGen | 多 agent 对话编程 | `_single_threaded_agent_runtime.py::_process_next`（actor 队列） | 计划只在 Magentic-One 的文本账本里 |
| LangGraph | —（无论文） | `_algo.py::_triggers/prepare_next_tasks`、`_loop.py::tick/_put_checkpoint`（BSP＋checkpoint） | **"DAG 编排"是误解**；计划能力在更高层包 |
| LLMCompiler | 规划与执行解耦＋并行 | `task_fetching_unit.py::Task.dependencies` + `_get_all_executable_tasks`（**真 DAG 调度**） | "编译器"隐喻大于实际（无 IR、无优化 pass） |
| HuggingGPT | 任务规划＋模型选择 | `awesome_chat.py::fix_dep/unfold/run_task`（dep 数组＋线程调度） | "通用任务求解"实为固定任务类型模板 |
| Plan-and-Solve / SayCan / ADaPT / DEPS / ReWOO | — | **未核实**（官方可运行实现未定位） | **全部**——引用时只能作为论文主张 |

**读表要点**：②栏空白的 5 项不能再被当作"已实现的机制"引用；③栏最值得警惕的两条是 **OpenHands 的 PLAN.md（有文件≠有计划）** 与 **LangGraph 的 DAG 叙事（有图≠DAG 调度）**。唯一"论文说的结构==代码里的结构"的是 **LATS（树）** 与 **LLMCompiler（DAG）**——这两个是 Atlas 数据模型最可靠的参照物。

### 3.4 结构选型决策表

判据五个：**可分解性、依赖密度、可回滚性、单步成本、观测确定性**。

| 任务特征组合 | 推荐结构 | 理由 | 反例警告 |
|---|---|---|---|
| 可分解性低＋依赖密度高＋单步成本低 | **线性 L1** | 拆不开就别硬拆；线性最省 token、最易 replay | 强行 DAG 会引入无法验证的伪依赖 |
| 可分解性高＋依赖密度中＋观测确定 | **DAG L2**，静态计划＋动态调度 | 依赖可并行、可缓存的唯一场景 | 计划生成后不改＝过早固化 |
| 可分解性高＋可回滚＋单步成本高 | **树搜索 L3** | 高成本单步必须"想清楚再动手" | 环境不可回滚时，树搜索要付真实副作用 |
| 可分解性低＋观测不确定＋需多轮试错 | **反思循环 L0** | 没有可分解结构，只能整段重来＋语言反思 | Reflexion 式整段重跑在长任务上成本线性爆炸 |
| 可分解性高＋子任务异构＋依赖稀疏 | **分层 L4** | 每层用不同验证器；层间契约稳定 | 层数 >2 时上层账本退化成"一段长文本" |
| **长程（>30 步）＋任一特征** | **分层＋step 级持久化**（Atlas 目标形态） | 只有 step 级状态能在崩溃后精确续跑 | 照搬 LangGraph 的 channel 版本模型会把"计划"埋进数据流 |

三条可操作判据：① **"这一步失败了，我能不能只重做这一步？"** 能→至少要 step 级状态；不能（副作用不可逆）→必须树搜索或人工确认点。② **"两个子任务之间有没有数据依赖？"** 有且多于 1 条→DAG；全是顺序依赖→线性，别过度设计。③ **"计划写错了我怎么知道？"** 每步有可执行的 success predicate→可自动重规划；没有→只能靠 LLM 自评，必须有人/审计器兜底。

### 3.5 重规划触发：信号 → 粒度 → 动作

| # | 信号 | 检测点 | 粒度 | 动作 |
|---|---|---|---|---|
| T1 | 确定性错误（参数非法、路径不存在、schema 失败） | `error.kind == invalid_input` | step 重试（换参数不换计划） | 同 step 新参数重试 |
| T2 | 瞬时错误（超时、限流、5xx） | `kind == transient` | step 重试（退避） | 指数退避；**幂等键不变** |
| T3 | 同一 (action, observation) 重复 N 次 | 哈希最近 N 步判重 | step 重试 → 换策略 | 先改参数再换工具 |
| T4 | 同一失败 action 连续 K 次 | `action_error_streak` | 换策略 | 禁用该 action/tool |
| T5 | **验证失败**（无异常但谓词 false） | `verify(step)` | **子目标重规划** | 只重写该 step 及下游输入绑定，不动无关分支 |
| T6 | **连续 N 次无进展** | `stalls += 1` / 否则 `-= 1`（迟滞） | 子目标 → 整计划 | 软阈值子目标级；硬阈值整计划重写并记 Decision |
| T7 | **状态不一致**（前条件不再成立/依赖产物被外部改） | 执行前重算 `precondition_check` | 子目标重规划（回到最近稳定 step） | 下游置 `stale`，从该点重展开 |
| T8 | 预算超限 | `Budget.exhausted()` | 整计划降级 | 更粗粒度计划，记 Decision |
| T9 | **依赖环/前向依赖** | 入图时拓扑排序失败 | **计划驳回** | 打回 Planner，禁止静默丢依赖 |
| T10 | **成功谓词不可判定** | 计划校验期 `verify` 为空 | **计划驳回** | 要求补 success predicate 或标"需人工确认" |

**粒度阶梯**：`step 重试 → 换策略 → 子目标重规划 → 整计划重写 → 计划驳回`。原则是**能用轻的就不用重的**；每次升级必须留一条 Decision 节点，否则 replay 无法解释行为。

**何时必须重规划而不是重试**，一条可操作分界线：

> **失败的原因在"计划的前提"里 → 重规划；在"这次执行的参数/环境瞬时状态"里 → 重试。**

满足以下任一即必须升级：失败信号指向某 `precondition` 为假；连续两次重试后 `verify` 仍 false 且两次 action 等价；失败 step 的上下游出现**事实冲突**（新观测与图记忆里的 Fact 矛盾——这是 Atlas 图记忆可以直接贡献、其他工作都没有的判据）；预算耗用超过该 step 预估 3×。

### 3.6 计划可执行性：五道闸

计划进 EXECUTING 前必须全过，任一失败回 PLANNING，**不允许带病执行**：

1. **前条件可满足**：每 step 声明 `preconditions`（工具/文件/凭据/外部状态），每条能对当前环境求值（真/假/未知三值）。为"未知"的必须带一个探测 step 作为首个前置动作。
2. **依赖无环**：拓扑排序失败即驳回（T9）；禁止前向引用；上游 `produces` 必须覆盖下游声明的输入（类型/形状匹配）。
3. **每步有可验证的成功谓词**：`verify` 必须是**可执行判定**（文件/退出码/schema/断言），分三档 `executable | heuristic | llm_judge`（llm_judge 必须记为低置信并允许人工推翻）。无 `verify` 一律 `needs_human`（T10）。
4. **预算可行**：`Σ 每步预估 ≤ 剩余 Budget`，且预留至少一次 hard 级重规划空间；预估缺失按历史同类 p50，连续 3 次超 2× 触发 T8。
5. **可观测**：每 step 的 observation 必须落 event log 且可被 `verify` 读取。

第 3 条是整个模型的关键取舍：**`verify` 是必填字段，不是可选装饰**。这一条直接消灭了"计划看起来可执行、实际无法判定完成"的大部分问题——也正是 Atlas 现有 Auditor 的弱点（只在 `audit.refs` 非空时校验）。

### 3.7 对 Atlas：Step / Dependency / Scheduler 最小模型

**Step**（最小执行单元，一行一记录）：`step_id`（`{run_id}:{seq}`）、`plan_id`、`parent_step_id`（支持 ADaPT 式按需展开）、`title`、`kind ∈ {tool_call, llm_call, verify, human_gate}`、`inputs`（含 `${stepID.output.field}` 引用）、`outputs`（声明产出，供下游匹配）、`preconditions`、`verify{mode}`、`budget`；状态 `status ∈ {pending, ready, running, done, failed, stale, skipped, needs_human}` ＋ `attempt` ＋ `stall_score` ＋ `failure_kind` ＋ `idempotency_key`。

**Dependency**（独立表，**不要复用 `EdgeRow`**）：`from_step`、`to_step`、`kind ∈ {data, order, verify, retry_of}`、`binding`（data 边必须说明"from 的哪个输出字段 → to 的哪个输入参数"）。入图强制不变式：无环、无前向引用、`produces ⊇ consumes`、data 边 binding 非空。**理由**：`EdgeRow` 是语义记忆（Constraint/Decision/Fact/Artifact 之间的关系），Dependency 是控制流；混表会让 replay 无法区分"知识关系"与"执行顺序"。

**Scheduler**（纯函数决策，不执行）：`ready_steps(plan, state)`、`next_action(ready, policy)`、`should_replan(plan, signals)`。调度器只产出派发指令，执行由 Worker 承担、结果回写 event log 再驱动下一轮——这个"决策与副作用分离"让调度器可以离线单测（正好吃现有 fixture＋故障注入）。并行度只来自 data 依赖的独立性；`order` 依赖串行，不要为它引入并发。每轮调度写 checkpoint，但**内容是"计划＋各 step 状态"，不是通道版本**——这是 Atlas 与 LangGraph 的根本差异。

**StepStateMachine**（在现有 `TaskStateMachine` 之外新增）：`pending → ready → running → {done|failed|needs_human}`；`failed → ready`（重试）、`failed → stale`（被上游波及）、`stale → ready`（重绑定）、`done → stale`（T7 状态不一致时已完成也要失效）。非法迁移必须 `record_violation`（现成机制）。**Task 级状态机保留但降级**：`TaskState` 只表达"这个 run 在哪个阶段"，具体推进由 step 状态机聚合得出，这样 `to_checkpoint/from_checkpoint` 不用大改。

**可直接借鉴**：LLMCompiler 的 `Task.dependencies`＋就绪判定（→ `Dependency.binding`＋`ready_steps`）；LangGraph 的"版本＋已见版本"用于幂等重放判定、`interrupt_before` 的暂停语义；LATS 的 `Node.exhausted`＋"回退到最近未耗尽祖先"（→ REPAIRING 的回溯）；OpenHands 的 `stuck_detector` 四类模式（最工程化的触发器实现）；Magentic-One 的 stall 计数**带衰减**（`max(0, n-1)`，抗抖动）；SWE-agent 的 `StepOutput` 六字段；Voyager 的"技能＝可执行代码＋描述＋检索"三元组（→ Artifact 加可执行性元数据）；ADaPT 的"失败才分解"（→ `parent_step_id` 自引用）；ReWOO 的变量引用式依赖。

**不能照搬**：① LangGraph 的"计划＝代码里写死的图"——Atlas 的计划必须是**数据**，能在运行中被审计、局部修改、replay 重放；② Voyager 的"失败即跳过"——与"可靠完成任务"直接冲突；③ HuggingGPT 的"丢弃非法依赖"（`if dep_id >= task["id"]: task["dep"] = [-1]`）——把计划错误变成静默降级，最坏的反模式；④ Reflexion/SWE-agent 的"整段重跑"——长任务上成本线性爆炸，只能借其**内容**（计划修正条目），不能借其**粒度**。

---

## 4. RQ2：长期记忆与上下文管理

> 完整 15 字段模板见 [`notes/research-2026-11/rq2-memory.md`](../notes/research-2026-11/rq2-memory.md)（13 个工作）。本节给综合结论与可直接落地的方案。

### 4.1 六类范式对比：分界线不在"存什么"，在"能不能表达失效"

| 维度 | ① 纯对话历史 | ② 摘要记忆 | ③ 向量记忆 | ④ 图记忆 | ⑤ 结构化状态记忆 | ⑥ 混合记忆 |
|---|---|---|---|---|---|---|
| 写入时机 | 每轮无条件追加 | 窗口满/边界一次压缩 | 抽取后逐条 embed | 抽实体关系建边 | **状态变迁时**同步写 | 热路径追加＋冷路径抽象 |
| 读取方式 | 全量拼 prompt | 摘要＋最近 N 轮 | 相似度 top-k | 图遍历 / PPR | **确定性列举**（按类型与活跃度） | 检索＋列举并行 |
| 容量成本 | 线性增长至超窗 | 有界但不可逆丢失 | 有界、便宜 | 建图贵 | 很小（条目数≈任务复杂度） | 最高 |
| 能否表达**约束失效** | ✗ | ✗ | ✗（旧条目仍在 top-k） | △ 可加时间戳，无强制语义 | **✓ 原生**（status＋superseded_by＋valid_until） | ✓（状态层负责） |
| 能否表达**事实冲突** | ✗ | ✗（静默合并） | ✗（都召回靠模型挑） | △ 需自定义冲突边 | **✓ 可检测并上报** | ✓ |
| 可审计性 | 强（有原文） | **弱**（不可溯源） | 中（chunk id） | 中 | **强**（source/confidence/verified_at） | 强 |
| 典型失败 | 超窗遗忘开头 | **关键细节被抹掉且无人察觉** | 相似≠正确、矛盾副本共存 | 建图贵、噪声大 | **写入不可靠→图与事实脱节** | 复杂、难调 |
| 代表 | chat log | MemGPT 递归摘要、MemoryOS 分段页 | Mem0、MemoryBank | HippoRAG、GraphRAG、A-MEM | **Atlas 自身**、Letta block、Memory-R1 | REMem、MemOS |

**读表要点**：③向量与④图的差别在**索引结构**；④与⑤的差别在**语义**——前四类回答"哪些片段与 query 相似"，⑤回答"此刻什么为真"。这是本 RQ 最重要的一条分界线。

### 4.2 核心判断：图记忆 ≠ 状态记忆（两者正交）

| | **状态记忆（state）** | **检索记忆（retrieval）** |
|---|---|---|
| 回答的问题 | 当前任务此刻**为真**的事实/约束 | 哪些片段与当前 query **相似** |
| 真值假设 | **唯一真源**（同 key 只应有一个活跃值） | 无真值概念，多副本共存 |
| 更新语义 | 就地变迁：supersede / expire / 版本指针 | 追加；旧条目留在索引里 |
| 失效表达 | 一等公民（status、valid_until、superseded_by） | **无法表达** |
| 冲突处理 | 检测并显式上报 | 两条一起召回，静默交给模型 |
| 读取 | 确定性列举，同一快照必得同一结果 | 概率性 top-k，索引漂移即结果漂移 |
| 成本结构 | **写入贵、读取极便宜** | 写入便宜、每次查询都要检索 |
| 失败模式 | 写入不可靠→图与事实脱节 | 矛盾副本共存→模型随机采信 |

**支撑证据（当状态管）**：

1. **Mem0 自己承认需要状态语义，又在默认路径上放弃了它**——这是全组最硬的一条证据。同一份代码里：`mem0/configs/prompts.py:176` 的 `DEFAULT_UPDATE_MEMORY_PROMPT` 声明 `ADD/UPDATE/DELETE/NO-CHANGE` 四操作，该 prompt 的四操作含 DELETE（允许删除与既有记忆矛盾的事实）；但 `:468` 的 `ADDITIVE_EXTRACTION_PROMPT` 写着 "Your **sole operation is ADD**"，而 `mem0/memory/main.py:944` 默认走后者。**规模化压力下，向量记忆主动退化成 append-only。**
2. **Memory-R1**（arXiv 2508.19828）把记忆管理显式化为 `ADD/UPDATE/DELETE/NOOP` 四操作状态机，用结果驱动 RL 学习何时迁移——论文明确指出既有管线"静态、启发式驱动"。
3. **Ground Truth First**（arXiv 2607.21962）给出字段级 `validity interval` + `volatility class` + `source channel`，用 as-of-date 问题集强制正确处理有效期。它的纵向实测有两条对 Atlas 直接有用：① **架构排名随历史长度反转**——预算受限的 curated-map 在 3 周领先，到 9 周对被淘汰内容的召回从 96% 掉到 72%，而 provenance-typed graph 升到 90%（六个用户同向，p=0.031）；② **写入质量是上游瓶颈**——写得弱的事实下游失败率 24%，写得强的只有 2%。
4. **Atlas 自己的 `provenance.py`** 字段集（`source/actor/confidence/valid_from/valid_until/verified_at`）与 Ground Truth First 的 fact 字段几乎同构——**方向被独立工作印证过，不是自创**。

**支撑证据（当检索管）**：HippoRAG（NeurIPS 2024）的 PPR 只是**索引**打分，`delete(docs)` 是删数据而不是标记失效；GraphRAG 的社区摘要是静态快照，无状态变迁；A-MEM（NeurIPS 2025）的 memory evolution 更新的是**表示**（context/keywords/tags），不是真值，没有 status、没有版本链；MemoryBank（AAAI 2024）用遗忘曲线决定留存——而"很久没提"与"不再为真"正交，**用权重表达失效是类别错误**。

**结论**：Atlas 走"图 + 状态"的组合，文献上最接近 Ground Truth First 的 provenance-typed graph 与 REMem 的混合图。**要防的是把图退化成索引**——只做相似度召回、不维护 `status`，就退回 HippoRAG 那一格，失去"唯一真源"的唯一卖点。

### 4.3 对 Four-Zone 的具体吸收方案

> 对照 `atlas/context/assembler.py`（`ZONE_PLAN/ZONE_SUMMARY/ZONE_RECENT/ZONE_HINTS`，shares=0.30/0.55/0.15，floors=300/400/120）与 `carryover.py` 的现有实现。

**区① 当前计划与约束**——保持现有顺序（目标→依赖序子任务→活跃约束→关键决定→环境事实→产出物→风险段），因为 [Lost in the Middle](https://doi.org/10.1162/tacl_a_00638)（TACL 2024）证明相关信息在开头/结尾最可信，而计划区正是"必须被遵守"的内容。新增三条：

- **per-type 上限**（借 Letta block 的 `limit`）：Constraint ≤ 计划区 40%、Decision 40%、Fact 20%，防止 Fact 挤爆其余；超限仍抛 `PlanZoneOverflow`，不静默裁。
- **写入闸门前移**：`provenance.missing_provenance()` 从"组装时过滤"改为"入图前拒绝"；同 key 冲突**不覆盖**，而是双方标 `meta["conflict_with"]` 并进风险段。
- **NOOP 出口**（借 Memory-R1）：extractor 允许输出"本轮无状态变化"，避免为凑数写噪声节点。

**区② 已完成摘要**——守住"摘要只叙事、不承载状态"（现有 docstring 已如此）。补两点：每条摘要标注**被摘要的节点 ID 列表**（借 Generative Agents 的 reflection 反向引用）使摘要可溯源；机械校验——摘要里出现的 `[C#]/[D#]` 必须真实存在于图中。裁剪从"最旧先删"改为**三段打分**：被活跃约束/决定引用的段落**钉住不删** → 其次是被 recall 过的 → 最后才按时间。

**区③ 近期原始记录**——唯一不需要校验的区，可逐字保留；按信息密度分层（工具报错、路径、命令、退出码完整保留，其余压一行）。**允许互相矛盾**，这是它的价值：它记录了"当时确实观察到 X"，解释权归区①。份额随结转相位动态调整（刚结转完区③为空，把份额让给区②）。

**区④ 踩坑与提示**——从"F3 已过期"升级为"**F3（旧值 X）已被 F7（新值 Y）接替，原因：工具返回 Y**"，并补反思性归因（借 Reflexion）。hint 必须能指回具体节点 ID 否则丢弃；措辞必须显式写"已失效，勿用"，防止模型把历史错误当现行规则。

**跨区三条硬规则（防"检索命中当状态真值"）**：

1. **写入闸门唯一**：任何外部文本进入区①的唯一通道是"候选 → 校验（source/confidence/有效期）→ 状态节点"，**检索器没有直接写图权限**。
2. **读取通道分离**：区①只做**确定性列举**，永不接受相似度排序；相似度只用于区③④的补充。
3. **真值唯一性检查**：`_audit()` 已保证活跃约束/决定必须出现；再补一条"同 key 活跃节点 > 1 且未标 conflict 即报错"，把静默二选一变成硬失败。

### 4.4 记忆评测：为什么 recall 不是好指标

LongMemEval（ICLR 2025）的五项能力是：信息抽取、多会话推理、时序推理、**知识更新**、**弃答**。后两项根本不测召回——"知识更新"测的是新值覆盖旧值后答新值，"弃答"测的是信息不在记忆里时该说不知道。**检索全命中但答旧值，recall=1、正确率=0。** 再加三条：命中≠被使用（Lost in the Middle）；命中≠更新（新旧都召回时模型可能采信旧值，此时命中率甚至因多召回而上升）；单 horizon 结论会反转（Ground Truth First）。

**建议的消融 6 组**：A 全量 / B 关图记忆 / C 关摘要 / D 只留最近 N 轮（N∈{5,10,20}）/ E RAG-only / F 关 provenance。**必加三个维度**：horizon 分桶（禁止只报总平均）、写入质量分桶（注入弱写入，验证 24% vs 2% 是否复现）、冲突注入（中途给一条与原约束矛盾的新约束，看是否**显式上报**而不是静默选一条——这是 Atlas 最有区分度、而现有基准都没有的测试）。

---

## 5. RQ3：环境状态感知与变化适应

> 完整 18 个工作模板见 [`notes/research-2026-11/rq3-environment.md`](../notes/research-2026-11/rq3-environment.md)。

### 5.1 状态维护光谱：状态放在谁那里

分档：**(a)** 有显式环境状态表示且有一致性检查；**(b)** 无状态、每步重新观测；**(c)** 有 observation→action→verification 闭环；**(d)** 声明有状态、实现里没有。

| 档 | 系统 | 证据 | 判定 |
|---|---|---|---|
| **a（agent 侧）** | **无** | — | **空白** |
| a（环境/评测器侧） | OSWorld | `desktop_env/desktop_env.py`：`snapshot_name="init_state"`、`_revert_to_snapshot()` | 有状态，但只服务 `reset()`，与推理脱钩 |
| a（评测器侧） | τ-bench | `tau_bench/envs/base.py`：`get_data_hash()`=sha256(data)，`calculate_reward()` 用 GT 动作重放后比哈希 | 有状态模型，属评测器 |
| a（评测器侧） | AppWorld / TheAgentCompany | DB 在 `data/tasks/<id>/dbs`；`api_calls.jsonl` 全量落盘；每任务 docker 含 `/utils/eval.py` | 有状态＋审计，属评测器 |
| **b（主流）** | WebArena、BrowserGym、SWE-agent、Voyager、AgentBench、ReWOO | 每步 `_get_obs()` / `communicate()` / `/step` 转发 | **11/18 纯重观测** |
| **c** | **BrowserGym**（唯一每步验证） | `post_step()`→`_task_validate()`，且**验证前后备份/恢复 `page` 与 `page_history`** | 闭环样板 |
| c（弱） | Voyager（critic 读 events 判 onError）、Reflexion/LATS（trial 间） | — | 闭环但粒度粗 |
| **d** | **OpenHands** | `_pending_action_info` 只在控制器内存；60s 超时**只写日志**（注释 "just for logging purposes"、"don't clear it"）；`State` 类无 pending 字段 | 声称跟踪、不可恢复 |

**光谱判读**：横轴是"状态放在谁那里"。放环境那里→agent 无法判断自己看到的与真实是否一致；放评测器那里→只有判分能力，没有在线纠错；放 agent 那里→**目前只有 OpenHands 试过，且没落盘**。**Atlas 要占的是第四格。**

### 5.2 关键否定事实：全行业都没有"未知提交状态"

逐系统证据（均为本次代码取证）：

| 系统 | 动作已发出的表达 | 未知结果的处理 | 失败分类 | 幂等键 | 对账 |
|---|---|---|---|---|---|
| WebArena | 无 | 异常→`info["fail_error"]`，`terminated=False` 继续 | 无 | 无 | 无 |
| BrowserGym | 无 | 异常→`last_action_error`，TimeoutError 特判 | 仅超时 | 无 | 无 |
| OSWorld | 无 | `step()` 无 try，异常冒泡中断 | 无 | 无 | 仅 snapshot 复原 |
| τ-bench | 无 | 异常 `str()` 成 observation | 无 | 无 | **判分时 GT 重放对账** |
| AgentBench | 有 `session_id` | `/interact` 网络异常→直接返回 `NETWORK_ERROR`，**不 cancel、不查询** | **5 类** | 无 | **无（漏洞）** |
| SWE-agent | 无 | `check ∈ {ignore,warn,raise}`；`raise` 时先 `close()` 销毁环境再抛 | 3 档 | 无 | 无 |
| OpenHands | `_pending_action_info`（内存） | 60s 仅 log；`State` 无 pending 字段 | ERROR/RATE_LIMITED/STUCK | 无 | 日志反推 `_is_awaiting_observation()` |
| AppWorld | 无 | 错误串返回＋closed-db 特判＋`max_interactions` 熔断 | 无 | 无 | `api_calls.jsonl` 事后审计 |

**读表**：所有系统都能表达"成功"和"确定失败"，**没有一个能表达"不知道"**。AgentBench 是唯一拿到 `session_id` 这种可反查把手的系统，却在网络异常分支把它丢掉了。

### 5.3 工程侧已经把这个问题解透了，agent 社区没接

以下是本次实际抓取正文并命中原话的来源（可直接引用）：

- **Temporal 三种超时 + Heartbeat**（`docs.temporal.io/encyclopedia/detecting-activity-failures`）：`Schedule-To-Start`（排队到开工）、`Start-To-Close`（单次执行上限，**无默认值，两个都不设则 Temporal 拒绝该 Activity**）、`Schedule-To-Close`（总时长）、`Activity Heartbeats`（执行中报活）。**这就是 PendingToolCall 超时三元组的原型**——把"没开工"、"开工了但没结束"、"总超时"分开，才能区分"肯定没执行"与"可能执行了"。
- **Stripe idempotency**（`docs.stripe.com/api/idempotent_requests`）："**Stripe saves the status code and body of the first request made with a given idempotency key after endpoint execution begins**"——服务端在"开始执行后"就存结果，重放请求能拿到第一次的响应。**这是"未知提交"的服务端解法。**
- **IETF Idempotency-Key 草案**："The idempotency key MUST be unique and MUST NOT be reused with another request with a different request payload." 另有两节分别讲 **Fingerprint**（同键不同 payload = 冲突）与 **Validity/Expiry**（键需要 TTL）。
- **Saga / Compensating Transaction**（Azure 架构中心）："If a step fails, a series of compensating transactions undoes the changes that the completed steps made." 且明确 **"Define the steps in a compensating transaction as idempotent commands"**——**补偿自己也会失败，所以补偿必须幂等**。
- **Transactional outbox**（microservices.io）：业务库更新与消息发送不能原子 → 写前日志（本地事务）与真正发出请求必须分开。
- **DBOS**：step 粒度即 checkpoint 粒度，"automatically recovers its execution from the last completed step"。
- **Fencing token**（Kleppmann）："include a fencing token with every write request... a number that increases every time a client acquires the lock"——**避免按过期状态继续执行的最强机制不是让 agent 自觉，而是让下游拒绝陈旧写**。

### 5.4 对 Atlas：PendingToolCall 状态机

这是本节唯一需要"新造"的数据结构。建议如下（文字模型，不是代码）：

**状态枚举**：`PREPARED`（已决定调用，未发出）→ `SUBMITTED`（已发出，未收到响应）→ `ACKED`（收到成功响应）→ `VERIFIED`（执行后重新观测确认副作用已生效）；旁支：`FAILED_TERMINAL`（确定失败）、`UNKNOWN`（超时/断连，**结果未知**）→ `RECONCILED_OK` / `RECONCILED_ABSENT` / `RECONCILED_PARTIAL`。

**转移与超时**（照抄 Temporal 三元组）：

- `SUBMITTED --StartToClose 超时--> UNKNOWN`（可能已执行）
- `SUBMITTED --ScheduleToStart 超时--> FAILED_TERMINAL`（肯定没开工，可安全重试）
- `SUBMITTED --收到明确 4xx--> FAILED_TERMINAL`
- `SUBMITTED --收到 5xx/429--> FAILED_RETRIABLE`
- `UNKNOWN --对账：查到操作已生效--> RECONCILED_OK --重新观测--> VERIFIED`
- `UNKNOWN --对账：查到未生效--> RECONCILED_ABSENT --可重试--> SUBMITTED`
- `UNKNOWN --对账不可判定--> 保持 UNKNOWN + 升级给人（HITL）`

**幂等键设计（须同时修现有实现）**：`build_idempotency_key(session, subtask, attempt=1)` 现状是**唯一调用点从不传 attempt**（`tools/protocol.py:112`），所以今天同一子任务的所有重试**共用** `…:attempt-1`——这点是对的（重试必须同键）。真正的缺陷是三项：① 函数签名邀请调用方把 attempt 编进键（`build_idempotency_key` 出现在 `tools/__init__.py:17,32` 的公开导出里，一旦有人传 `attempt=2` 幂等立即失效）；② **没有 payload fingerprint**，同一 `(session, subtask)` 被重规划换了 query 时会错误去重；③ **没有 TTL**。建议：键 = `hash(session, run_id, step_id, tool_name, canonical_payload)`，`attempt` 只作独立计数字段，另存 fingerprint 与 `expires_at`。

**与 checkpoint/replay 的关系**：PendingToolCall 必须进 checkpoint（当前 `CheckpointRow` 无此字段）；replay 时 `SUBMITTED` 状态**不得重放为"重新发出"**，必须先走对账。这条是"事件只追加 + 快照"架构下最容易出错的地方。

**观测失效（避免按过期信息行动）**：给每条环境观测加 `observed_at` + `source_fingerprint`（例如页面 ETag、文件 mtime+hash、DB 行版本号）；行动前校验 `observed_at` 是否晚于最近一次外部变更事件，或直接用 fencing token 让写操作在下游被拒。**没有 fingerprint 的观测不能用于决定有副作用的动作**——这条可以直接写进 Verifier。

---

## 6. RQ4：可靠性、恢复与长程任务评测

> 完整分析见 [`notes/research-2026-11/rq4-reliability.md`](../notes/research-2026-11/rq4-reliability.md)（13 个 benchmark）。

### 6.1 证据分层：哪些机制有论文证据，哪些只有框架文档

这是本 RQ 最该被写进 Limitations 的结论：

| # | 机制 | 解决什么失败 | Atlas 现状 | 证据档位 |
|---|---|---|---|---|
| 1 | checkpoint | 崩溃/中断后重来 | 有，但只在 advance/pause/resume | 工程实现（LangGraph `BaseCheckpointSaver`） |
| 2 | retry | 瞬时故障 | **策略类写好未接线** | 工程实现 |
| 3 | idempotency | 重复副作用 | 三套键；编排路径无键 | 工程实现 |
| 4 | compensation | 撤销已执行步骤 | **无** | 仅分布式系统经典（Saga） |
| 5 | rollback | 回已知好状态 | 部分（只作废节点，不回滚环境） | 工程实现（环境级快照） |
| 6 | partial completion | 全成/全败太粗 | **有数据结构无分档** | **论文机制**（TheAgentCompany checkpoint 给分、AgentBoard progress rate） |
| 7 | failure isolation | 单点拖垮全局 | 有（五类错误隔离） | 工程实现＋TRAIL 分类学 |
| 8 | replay | 事故不可复现 | 两套且都不完整 | 工程实现 |
| 9 | trace | 无法归因 | 有但不闭合（成功路径不写） | 工程实现＋TRAIL |
| 10 | budget | 无限烧钱 | **仅有类** | 产业惯例（无对照实验） |
| 11 | HITL | 高风险需人批 | 无审批语义 | 待核实 |
| 12 | verifier | 自评不可信 | 有但弱 | **强证据**（SWE-bench 测试、CORE-Bench 复现、τ-bench DB 终态） |
| 13 | self-correction | 一次做不对 | 有 bounded repair | **正反皆有**（ICLR 2024《LLMs Cannot Self-Correct Reasoning Yet》为反方） |
| 14 | bounded repair | 无限修复循环 | 有 `max_repairs=1`，但 API 路径不生效 | 工程实现 |
| 15 | at-least-once / exactly-once | 副作用重复或丢失 | 请求级近似 | **仅宣传/待核实** |

**三条读表结论**：

1. **只有 verifier 与一致性度量有硬论文证据**；checkpoint/retry/幂等/补偿/exactly-once 在 Agent 领域**没有同行评审对照实验**——写论文应放 Implementation，不能当 Contributions。
2. **"类写好了但没接线"是 Atlas 的系统性问题**，不是个别遗漏（`RetryPolicy`、`Budget`、`build_control_flow_graph`、`expire()`、`ContextAssembler` 在编排路径上全为零调用）。任何"我们实现了 X"都必须附一条生产路径调用栈。
3. **exactly-once 不要碰**。工具副作用不可撤销，公开文献无 Agent 级实现。稳妥表述是「at-least-once 投递 + 幂等键 + 可观测重复率」。

### 6.2 两个非标准指标的落地定义

**恢复成功率**（Atlas 最有差异化、也最容易自欺）：

- 样本单位＝一次**故障注入事件**，不是一次 run（长任务否则被稀释）；注入未真正生效的样本**剔除**，不记 0 也不记 1。
- 故障五类：工具瞬时（timeout/5xx/429）、工具永久（no_evidence/404）、进程中断（kill -9 重启）、上下文超限（组装溢出）、上游模型错误输出（非法 JSON）。
- **成功恢复＝三个条件全满足**：a) 任务层仍达最终成功判据；b) 状态层与"无故障对照 run"在关键节点集合上一致（**没丢步、没重复副作用**）；c) 成本层额外消耗 ≤ 失败前已消耗的 50%，否则单列 `recovered_degraded`。
- **无样本报 `None`，不报 1.0**（现在 `metrics.py:39-41` 正是记 1.0）。

**过期信息误用率**（两档都报）：

- **档 A 决策级（主指标）**：样本＝"存在值被更新"的决策点；分子＝**行为层面**以旧值为依据的任务（断言值==v1 且 ≠v2），**不看文本是否提到 v1**；分母＝该决策点被实际触达的任务。判据三元组：`asserted==v1 AND v2∈history AND task_requires_F`。
- **档 B 表述级（辅助，现口径）**：答案出现过期值且未给现值——只能当粗筛上界。
- 配套报 `stale_exposure_rate`（模型是否看到过期值，衡量检索器是否把过期内容送进上下文）与 `correction_rate`＝1−误用率/暴露率。

（档 A 的口径与 LongMemEval 的 knowledge-update、τ-bench 的政策遵守同源，因此**不是纯自创**，可以引。）

### 6.3 统计功效：为什么 5 个自建场景不可能支撑结论

- 比例类必须报 **Wilson 区间**（n 小或 p 接近 0/1 时正态近似会越界）。**40/40 的 95% CI 下限约 0.912**。
- **独立两臂检测 60% vs 70%（α=0.05, power=0.8）每组约需 356 个任务**。5 个自建场景只能做机制演示。
- 报告一致性用 **pass^k**（τ-bench 原文定义，已读正文核实）：`pass^k = E_task[C(c,k)/C(n,k)]`，`pass@k = 1 − E_task[C(n−c,k)/C(n,k)]`；要求 k≤n；τ-bench 设定 agent T=0.0、user simulator T=1.0、每任务 ≥3 trials；gpt-4o 在 τ-retail pass^1≈61%、pass^8<25%。
- **A/B 用配对设计 + McNemar**（只统计 discordant 对），样本效率远高于独立两臂。
- **temperature=0 只证明"基础设施不抖"**，不证明鲁棒。建议三档报告：T=0,n=2（可复现）/ T=1,n=8（pass^k 鲁棒）/ T=1,n=8 换 seed（敏感性）。
- 连续指标报中位数与 IQR 或 bootstrap 区间，**不要报均值±标准差**（长尾会骗人）；多 arm 多指标要么预注册主指标，要么 Holm 校正。

### 6.4 循环论证怎么破：换数据不换方法

Atlas 现有自评被外人视为循环论证的六条，及对应解法：

| 问题 | 外人会怎么说 | 解法 |
|---|---|---|
| 自建 fixture + 自写 probe | "你测的是你的 fixture" | probe 改结构化断言；**换 LongMemEval/τ-bench 官方脚本** |
| 检索器按自写 ranking 返回 | `coverage_ok` 必然成立 | 期望集合用独立金标 |
| 脚本替身扮演研究者 | 判分与生成同源 | head-to-head 同条件对比 |
| 同一模型既选手又裁判 | 闭环 | 固定第三方 judge + 报 judge-human κ |
| 基线被削弱（摘要只留 2 条） | 0.000 是设计使然 | 论文里明写"基线为下界" |
| 恢复率无样本记 1.0 / 延迟合成 | "没做实验宣称 100%" | 改 `None`；延迟实测或删除 |

**成本排序**（从低到高）：① 修判分口径 bug（1–2 天）→ ② 预注册 + 冻结（2–3 天）→ ③ head-to-head 同条件（1 周）→ ④ 接 LongMemEval（1–2 周）→ ⑤ 接 τ-bench retail 115 题（2 周）→ ⑥ 故障注入协议 + 恢复率（3–4 周）→ ⑦ 公开复现包（1 个月）→ ⑧ 第三方复现/官方榜。

**其中有一个免费的外部依据值得单独提**：**WebArena Verified**（SEA @ NeurIPS 2025 Poster）复审全部 812 个任务后指出"目标欠定义 + 检查器脆弱会系统性错估分数"，修法是类型/归一化感知比较器 + 状态变更任务查后端状态 + 结构化状态码 + 95% CI。这把 Atlas 自曝的"probe 过宽"从**工程瑕疵**升级为**已知方法论缺陷且有公开解法**——对答辩非常有用。

---
## 7. 综合：Atlas v2 的六个缺口与六个机制

### 7.1 一张对照表：缺口 → 机制 → 借鉴来源 → 验收

| # | 缺口（§2.5） | 补它的机制 | 主要借鉴 | 最小改造 | 怎么验收 |
|---|---|---|---|---|---|
| 1 | Planner 只输出 `{route, risks, reason}`，无 step/依赖 | **Step + Dependency + Scheduler**（§3.7） | LLMCompiler 的 `Task.dependencies`＋就绪判定；ADaPT 的按需展开 | 新表 `StepRow/DependencyRow`；Planner 输出改结构化；调度器做成纯函数 | 同一任务在 step 级 kill -9 后能从 `pending/ready` 续跑；局部重规划只重跑受影响子图 |
| 2 | 无 PendingToolCall，断线后不知外部是否执行 | **PendingToolCall 状态机 + 对账**（§5.4） | Temporal 三超时＋Heartbeat；Stripe 的"执行即存结果"；fencing token | checkpoint 加 `pending_tool_calls`；工具层加 `UNKNOWN` 态与 `reconcile()` | 注入"响应丢失"后，系统不重复下单且能给出 `RECONCILED_*` 结论 |
| 3 | 四区上下文只在 memory-server 路径；编排拿 5 行摘要 | **ContextAssembler 接到编排路径**＋写入闸门前移 | Lost in the Middle（顺序）；Letta block limit；Ground Truth First（写入质量） | `RepositoryGraph.state_view` 改调 assembler；provenance 门禁入图前 | 编排路径的 prompt 里活跃 Constraint/Decision 100% 出现（`_audit` 已能测） |
| 4 | Budget/Retry 是死代码 | **接线 + 分级预算** | LangGraph `_retry.py`；Magentic-One stall 迟滞 | `routes.py:166` 传 machine/budget；`_call` 包 `call_with_policy` | 人为让模型挂起，任务在预算内失败而非无限等 |
| 5 | Auditor 引用校验是可选强度 | **必填 `verify` + 三档强度**（§3.6） | SWE-bench/CORE-Bench 的"测试即判据" | `Step.verify` 必填；Auditor 的 `known_ids` 不可用时不放行 | 删掉一条证据后审计必须失败（现在是放行） |
| 6 | resume 只回状态机，不回上下文与在途调用 | **step 级 checkpoint + 恢复快照** | DBOS "从最后完成的 step 恢复"；LangGraph checkpoint 版本 | checkpoint 存计划＋step 状态＋pending calls；resume 重建四区 | 暂停→改文件→恢复，任务能感知外部变化并走 T7 |

**一条总原则**：Atlas 现有能力不是"缺"，而是"没接线"。上表 6 条里有 4 条（第 3、4、5、6）的主体代码已经存在，改造成本主要是**接线 + 补字段**，不是重写。这对进度是极大的利好，也意味着**对外表述必须先接线再说**。

### 7.2 Execution State 的三张表（建议的最小结构）

```
RunRow       : run_id, goal, status, budget, created_at, ...
StepRow      : step_id, run_id, plan_id, parent_step_id, title, kind,
               status, attempt, stall_score, failure_kind,
               preconditions(JSON), verify(JSON), budget(JSON),
               idempotency_key, started_at, ended_at
DependencyRow: from_step, to_step, kind, binding(JSON)
PendingToolCallRow: call_id, step_id, tool, payload_hash, idempotency_key,
               status, submitted_at, deadline_at, reconcile_result(JSON)
```

四张表（含 Run）与现有 `SessionRow/NodeRow/EdgeRow/EventRow/CheckpointRow/RunMetricRow` 正交：**前者是执行状态，后者是知识状态与审计**。这与 `docs/atlas-harness-architecture.md` 的 Execution/Knowledge 二分一致，也把该文档从"口号"变成"schema"。

### 7.3 可恢复性：三档承诺，不要串档

| 档 | 承诺 | 靠什么 | Atlas 现状 |
|---|---|---|---|
| **任务级** | 崩溃后能回到上一次状态转移 | checkpoint + 状态机 | ✅ 有 |
| **step 级** | 崩溃后能续跑"当前 step 之前已完成的部分"，不重做已提交 step | StepRow + 每轮调度写 checkpoint | ❌ 缺 |
| **tool-call 级** | 崩溃/断线后知道在途调用是否已执行 | PendingToolCall + 对账 | ❌ 缺 |

**对外只能声明已经做到的那一档。** 现在能声明"任务级"，声称"精确恢复"会被审计击穿（`retry_count` 恒 0、resume 不恢复上下文）。

**事件溯源的边界**：Atlas 是"**只追加事件 + 状态快照 + 有限 replay**"，**不是事件溯源**。理由有三：`link` 事件不参与 replay（`events.py:45`）；图本体来自 `nodes` 表；`seq` 用 `max+1` 生成而非数据库序列。要变成真事件溯源，需要：事件全序（DB 序列）、`link` 进 replay、图完全由事件重建。**建议不要现在做**——收益低（现有 replay 已够定位问题），代价高。措辞上改为"append-only event log + snapshot-based replay"即可。

### 7.4 记忆：两层分离，写入闸门唯一

- **状态层**（唯一真源）：`Constraint/Decision/Fact/Artifact`，带 `source/confidence/valid_from/valid_until/verified_at`，用 `supersede/expire` 表达变迁。
- **检索层**（可多副本、允许过期）：CompassRAG 的 evidence，**永远先进候选区**，过闸门才可能成为 Fact。
- **三条硬规则**（§4.3）：写入闸门唯一、区①只确定性列举、同 key 多活跃即报错。

**这一层的关键动作只有一个**：把 `expire()` 接上（现在无调用者），并让 `supersede` 覆盖"Fact 被新证据推翻"（现在只有 Artifact 修订）。没有这两条，Atlas 的"状态记忆"只是"带时间戳的图"，退化成 HippoRAG 那一格。

### 7.5 环境：把"不知道"变成一等状态

Atlas 现在的 `ERROR_KINDS` 五类（`timeout/invalid_request/no_evidence/budget_exhausted/upstream_error`）**全是否定式失败**，没有 `unknown_commit`。而真实世界最常见的正是"发出去了但没收到回执"。

最小改造：① `ERROR_KINDS` 加 `unknown_commit`；② 工具层返回 `PendingToolCall` 句柄而不是直接抛错；③ `reconcile()` 用工具自带的查询接口（如订单查询、文件 stat、DB 读）判定是否已生效；④ 无法判定时**升级给人**（PAUSED + 待办），不猜。

### 7.6 评测：三步走，先止损再对外

| 阶段 | 动作 | 时间 | 得到什么 |
|---|---|---|---|
| **止损** | 修 10 个判分过宽点；`recovery_success_rate` 无样本返回 `None`；删合成延迟 | 1–2 天 | 内部数字不作废 |
| **可对内部** | 预注册指标 + n + seed 与判分器版本，打 tag；原始 trace 全量落盘 | 2–3 天 | 没有 HARKing |
| **可对外** | head-to-head 同条件（McNemar）→ LongMemEval（KU/弃答子集）→ τ-bench retail 115（pass^k, n=8） | 3–5 周 | 外人能信的起点 |

**必须放弃的三种表述**：① "40/40 完成率"（Wilson 下限 0.912，且 6 个样本没有统计意义）；② "延迟/成本下降 X%"（现值是 `synthetic_estimate`）；③ "self-correction 提升效果"（ICLR 2024 有反例，改说 "bounded repair with external verifier"）。

### 7.7 优先级与"不做什么"

**P0（两周内，全是接线）**：① `routes.py:166` 传 machine/budget；② `state_view` 改调 assembler；③ `_call` 包 `call_with_policy`；④ 编排成功也写 trace；⑤ 修 `repair_required` 且 issues 为空不收敛的路径；⑥ `expire()` 接一个真实调用点。

**P1（一个月，补数据结构）**：StepRow/DependencyRow/Scheduler/StepStateMachine；PendingToolCall + `unknown_commit` + 对账；checkpoint 扩字段；评测止损。

**P2（季度，做差异化）**：子目标级局部重规划（文献零先例，是真正的贡献点）；故障注入协议 + 恢复成功率；head-to-head 公开基准。

**明确不做**：① 通用多租户与生产级 SLA（无证据需求）；② exactly-once（无 Agent 级实现，代价极高）；③ 自研向量库/图数据库（用 SQLite/Postgres 足够）；④ 再做第二个"顶层 Agent 产品"（RAG 已降级为 Tool，不要再造同级产品）；⑤ 宣称"DAG 调度"直到 Scheduler 真落地。

### 7.8 三套对外口径（简历 / 论文 / 答辩）

- **简历（一句话）**：面向长程任务的**状态化、可恢复** Agent Harness；把 RAG 降级为可插拔 Retriever Tool，用 Execution/Knowledge 双状态分离解决长程执行的上下文与恢复问题。
- **论文贡献（三条，按可证性排序）**：① **计划作为一等状态**：step 级持久化 + 子目标级局部重规划（文献空白）；② **PendingToolCall 与未知提交状态**：把"不知道"变成可判定状态（工程共识 + Agent 领域空白）；③ **状态记忆 vs 检索记忆的边界**：写入闸门与失效语义（有独立证据支撑）。
- **答辩必须主动交代的三条**：① 现有 e2e 是离线 fixture，只证机制不证效果；② 40 步真实数字已撤回（fixture 预置未来事实 + probe 过宽）；③ 可靠性机制多数无同行评审对照，属工程实现。

---

## 8. 全量工作索引（15 字段模板位置）

| 分报告 | 覆盖工作 | 模板数 |
|---|---|---|
| [`rq1-planning.md`](../notes/research-2026-11/rq1-planning.md) | ReAct、Plan-and-Solve、ToT、LATS、Reflexion、SayCan、Voyager、SWE-agent、OpenHands、AutoGen、LangGraph、LLMCompiler、ADaPT、DEPS、ReWOO、HuggingGPT、Magentic-One | 17 |
| [`rq2-memory.md`](../notes/research-2026-11/rq2-memory.md) | Generative Agents、Reflexion、MemGPT/Letta、Voyager、Mem0、MemoryBank、HippoRAG、GraphRAG、LongMemEval、CoALA、A-MEM、Memory-R1、REMem、Ground Truth First | 14 |
| [`rq3-environment.md`](../notes/research-2026-11/rq3-environment.md) | WebArena、BrowserGym、OSWorld、τ-bench、AgentBench、SWE-bench、SWE-agent、OpenHands、Voyager、ReAct、Toolformer、LATS、Reflexion、ReWOO、CoALA、WorkArena、TheAgentCompany、AppWorld | 18 |
| [`rq4-reliability.md`](../notes/research-2026-11/rq4-reliability.md) | SWE-bench（含 Lite/Verified）、WebArena/WebArena Verified、OSWorld、τ-bench、AgentBench、GAIA、LongMemEval、TRAIL、AgentBoard、CORE-Bench、TheAgentCompany、RE-Bench、MAST | 13 |

**元数据核验表**：[`venue-verified.md`](../notes/research-2026-11/venue-verified.md)（38 条，含核验渠道与"待核实"标记）
**代码取证**：[`atlas-audit.md`](../notes/research-2026-11/atlas-audit.md)（21 项能力 × file:line × 覆盖范围）

---

## 9. 不确定与边界

### 9.1 明确标为"待核实"的条目

| 条目 | 状态 | 影响 |
|---|---|---|
| Plan-and-Solve / SayCan / ADaPT / DEPS / ReWOO 的**可运行官方实现**未定位 | 未核实 | 这 5 项只能作为论文主张引用 |
| HippoRAG 的 PPR 具体实现文件 | 未定位（`index/delete/retrieve` 已实读） | 不影响结论（PPR 作索引的判断来自论文＋代码骨架） |
| Letta block ORM 全字段 | 抓取超时（已确证 `block_manager.py:32` 字段集与 `:813` limit 强制） | 低 |
| MemOS 有两篇不同标题的 arXiv 记录（2505.22101 / 2507.03724） | 已确认是两篇不同论文 | 引用时必须写全标题 |
| GAIA 官方评分函数是否独立开源 | 未取到 | 只影响"能否复现"的表述 |
| CORE-Bench | 实为 **TMLR 接收**（rq4 原记 arXiv preprint，本轮 OpenReview 更正） | 已更正 |
| TheAgentCompany | arXiv 自述 Preprint，OpenReview 显示 **NeurIPS 2025 D&B poster** | 已按 OpenReview 记录 |
| τ-bench 的 ICLR 2025 归属 | Lead 与两名研究员结论不一致（Lead 命中 `ICLR 2025 Poster`） | 保留分歧记录 |
| "Agent 领域无 idempotency/compensation/budget/HITL/exactly-once 对照实验" | 是**检索结论**，非定论 | 正文应写"我们未找到" |

### 9.2 本报告的方法局限

1. **不是系统性综述**：没有 PRISMA 式检索与双人筛选，纳入标准是"机制相关"。
2. **索引受限**：OpenAlex/S2/DBLP 全部不可用，无法给引用数与影响力分析；venue 依赖 OpenReview + arXiv Comments + PMLR + Crossref + ACL Anthology 五种渠道交叉。
3. **未复现任何 benchmark**：所有性能数字均引自论文/README，未独立验证。
4. **GitHub 证据是"文件与符号存在"级**：未逐行审读全部实现，个别机制判断（如"是否为真 DAG 调度"）依赖关键函数阅读，可能仍有偏差。
5. **Atlas 侧结论绑定当前工作区 HEAD**（含未提交改动），行号可能随重构漂移。

---

## 10. 一个必须先处理的口径冲突

写这份报告时发现 Atlas 内部存在**互相矛盾的两个"当前证据"口径**，对外引用前必须先统一：

| 来源 | 说法 |
|---|---|
| `PROJECT.md` 第 3 行（v0.3.1，2026-10-28） | 「**旧 40 步真实模型数字已撤回**：原 fixture 预置未来事实且 probe 判分过宽，需按新版口径重新跑真实模型」 |
| `PROJECT.md` 第 143 行（2026-10-29 续十三） | 40 步**离线**完成率均 1.0，累计 prompt token = 230020/17005/3733；「延迟仍为 synthetic estimate，**真实模型需按新版口径重跑**」 |
| `LEDGER.md` §2 | 「主力证据（未变）：**40 步真实模型**，完成率 1.000/1.000/0.000；累计 token 244,381/62,680(−74%)」 |

**判断**：PROJECT.md 的撤回声明更晚（v0.3.1 是修口径后的状态），且与"续十三"的"真实模型需按新版口径重跑"一致。**台账里的"主力证据（未变）"是口径修复前的旧表述**。

**建议**：① 台账该行改为"离线 40 步：完成率 1.0/1.0/0.333（token 230020/17005/3733，synthetic latency）；真实模型数字已撤回，待重跑"；② 任何对外材料（简历/答辩/论文）在重跑完成前**不得引用 244,381/62,680 这组数**；③ 若要保留"−74%"这个卖点，必须先用新版口径重跑并给出 n 与 CI。

---

*本报告与四份分报告、代码审计、元数据核验表共同构成 2026-11 调研包。所有 venue 以 `venue-verified.md` 为准；所有 Atlas 代码结论以 `atlas-audit.md` 为准。*
