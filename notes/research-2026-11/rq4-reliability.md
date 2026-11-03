# RQ4 可靠性、恢复与长程评测

> 调研人：rq4-reliability（2026-11-04）。venue 一律以 `notes/research-2026-11/venue-verified.md`（Lead 核验）为准，本表另有自核的写明渠道。
> 证据规则：本文出现的 benchmark 数字、判分代码路径、许可证，**均来自本次会话内实际访问的工具返回**（GitHub raw/tree、arXiv abs/HTML、HF datasets-server、OpenReview API、仓库 README）。凡未取到的写「待核实」，不做记忆补全。
> 三条栏位约定：**论文机制** = 同行评审论文里给出方法与实验；**工程实现** = 公开代码/仓库里能跑的东西；**仅宣传** = 只有博客/框架文档或口头主张，无同行评审证据。

---

## 0. 结论摘要（硬结论）

1. **「长程 Agent 可靠性」在文献里已经被拆成三个可测的东西，但没有一个 benchmark 同时测三层**：①结果对不对（SWE-bench 用测试、τ-bench 用数据库终态、OSWorld/WebArena 用执行态校验）；②过程对不对（TRAIL 的 148 条人工标注轨迹 / 841 个错误、AgentBoard 的 progress rate）；③同等条件下稳不稳（τ-bench 的 pass^k）。Atlas 现在的评测只在第①层，而且用的是自建 fixture。

2. **判分器本身已经成为一个被研究的问题，不只是 Atlas 的私事**。WebArena Verified（SEA @ NeurIPS 2025 Poster）复审全部 812 个任务后指出：目标欠定义 + 检查器脆弱会**系统性错估**分数，其修法是「类型/归一化感知比较器 + 状态变更任务查后端状态 + 结构化 JSON 状态码 + 95% 置信区间 + 模板级宏平均」。这条正好把 Atlas 自曝的「自写 probe 判分过宽」从工程瑕疵升级为**已知的方法论缺陷**——别人也踩过，且有公开解法可引。

3. **可靠性机制里，只有「验证器」和「一致性度量」有硬论文证据**。SWE-bench（测试即验证器）、CORE-Bench（复现即验证器，270 任务/90 篇论文）、τ-bench（pass^k）是可信的三根柱子；而 **checkpoint / retry / idempotency / compensation / exactly-once 在 Agent 领域基本没有同行评审的对照实验**，证据主要来自框架文档（LangGraph/Temporal 类「durable execution」宣传）与分布式系统的经典结论（Saga/幂等）。这条必须写进论文的 Limitations，否则审稿人一戳就破。

4. **self-correction 不能当默认美德**。ICLR 2024 poster《Large Language Models Cannot Self-Correct Reasoning Yet》给出的结论是：没有外部反馈的自纠往往不升反降；而 Self-Refine（NeurIPS 2023）那类「自我打磨」的增益高度依赖任务与判据。Atlas 的 Auditor 打回重跑一次，属于「有外部判据的 bounded repair」，这条是有据可依的；但把它包装成「self-correction」会引来反例。

5. **多智能体/长程失败有公开分类学可对标**：TRAIL 的三层九类（推理错误 4 类 / 系统执行错误 3 类 / 计划协调错误 2 类）、MAST（NeurIPS 2025 D&B spotlight，《Why Do Multi-Agent LLM Systems Fail?》）。Atlas 若要写「failure isolation」，最省事的做法是把自建故障分类映射到 TRAIL 的类目上，而不是自创词表。

6. **τ-bench 的 pass^k 原文定义已核实**（读的是论文 HTML 正文，不是摘要）：pass^k = E_task[ C(c,k) / C(n,k) ]，pass@k = 1 − E_task[ C(n−c,k) / C(n,k) ]，其中每个任务跑 n 次、成功 c 次；**pass^k 要求「k 次全成功」**，pass@k 要求「至少一次成功」。原文实验设定：agent temperature = 0.0、user simulator temperature = 1.0、每个任务**至少 3 次试验**；gpt-4o 在 τ-retail 上 pass^1 ≈ 61%、pass^8 < 25%。

7. **Atlas 现有三臂数字（1.000 / 1.000 / 0.000，temperature=0，重复 2 次且两次完全一致）只能证明「基础设施不抖」，不能证明「方法鲁棒」**；且 PROJECT.md 第 3 行已声明旧 40 步真实模型数字**撤回**（fixture 预置未来事实 + probe 过宽）。按 Wilson 区间，40/40 的 95% CI 下限约 0.912（本报告算术），而「60% vs 70%」的两臂对比每组约需 356 个任务才有 80% 功效——**5 个自建场景在统计上不可能支撑任何效果结论**。

8. **走出循环论证的唯一低成本路径是「换数据集，不换方法」**：把 Atlas 的图状态记忆接到 LongMemEval（500 题、官方评测脚本 `src/evaluation/evaluate_qa.py`、ICLR 2025）与 τ-bench（retail 115 / airline 50、DB 终态判分、MIT）上做 head-to-head，判分器用官方脚本、指标预注册。自建 fixture 可以留着做**回归测试**，但不能留在论文的效果表里。

---

## 1. 可靠性机制对照表

说明：**优先级**按「对 Atlas 论文可信度的边际收益 / 改造成本」排序（P0 最高）。**证据档位**列区分本文最重要的三栏；标「待核实」的是本轮没取到公开对照实验的。

| # | 机制 | 解决什么失败 | 代价 | 真实现的证据（论文/工程） | Atlas 现状（file:line） | 优先级 | 档位 |
|---|---|---|---|---|---|---|---|
| 1 | checkpoint | 进程崩溃 / 长任务中断后重来 | 每次转移一次写库；快照体积 | LangGraph `BaseCheckpointSaver`（`libs/checkpoint/.../base/__init__.py:199`，见 §1.1）；**Atlas 自证**：`state_machine.py:23-55` 七状态 + `runtime.py:124/153/180` 三处写 checkpoint | 有，但**只在 advance/pause/resume**；编排内部步、工具调用、写图都不写（`service.py:299-320`）；`retry_count` 恒 0 | P0（补「当前步 + pending 工具调用」字段） | 工程实现（无同行评审对照） |
| 2 | retry | 瞬时故障（超时、429、连接断） | 放大尾延迟；需幂等配合 | LangGraph `pregel/_retry.py`（`_AttemptContext:88`、`on_retry:312`）；**Agent 领域无对照实验**（待核实） | **策略类已写好但未接线**：`policies.py:119-167` 的 `RetryPolicy/call_with_policy` 只在测试调用；`orchestrator._call` 无超时无重试（`orchestrator.py:409-413`） | P0 | 工程实现 / 生产未接 |
| 3 | idempotency | 重复提交导致重复副作用 | 需存结果 + 键管理 | 框架层惯例；Atlas 自证请求级 | 三套键（请求 `runtime.py:330-348`、工具 `tools/protocol.py:27-28`、节点 `models.py:53-56`）；**编排路径 `role_note` 无幂等键**（`api/graph.py:30-54`） | P1 | 工程实现 |
| 4 | compensation | 已执行步骤需要「反向操作」抵消 | 每个工具要实现逆操作；语义难定义 | 经典 Saga 模式（分布式事务），**Agent 领域无公开评测**（待核实） | **无**（全仓 grep 无 compensat*/undo*） | P2 | 待核实 |
| 5 | rollback | 错误分支需回到已知好状态 | 需状态快照 + 副作用不可逆时失效 | OSWorld/WebArena 的**环境快照与 reset**是最接近的工程证据（见 §2） | **部分**：`supersede` 只作废 Artifact 节点，不回滚环境（`repository.py:273-306`） | P2 | 工程实现（环境级） |
| 6 | partial completion | 长任务全成/全败的二值判分过于粗暴 | 需 checkpoint 式分档计分 | **TheAgentCompany**（175 任务、checkpoint 给分，论文正文出现「Checkpoint 1 (5pts)」）；AgentBoard 的 progress rate（0–1 连续） | **有数据结构但无分档**：`metrics.py:36` 完成率按整条任务二值；`evaluation/harness.py:28-47` 有 `StepResult` 但只用于离线 | P0（改判分口径，成本极低） | 论文机制 |
| 7 | failure isolation | 单个工具/角色失败拖垮整条任务 | 需要明确的降级路径 | TRAIL 的「系统执行错误」类目（API/系统/资源）；Atlas 自证 | **有**：工具错误五分类 `tools/protocol.py:13-22`，失败后写 trace、不写图、继续用原问题（`orchestrator.py:281-308`）；但**无备用工具链** | P1 | 工程实现 + 论文分类学 |
| 8 | replay | 事故无法复现，无法归因 | 需事件溯源或完整 trace | TRAIL（轨迹级错误定位，最好模型仅 11%）；Atlas 自证 | **两套且都不完整**：事件表 replay 只重建节点状态、`link` 不参与（`storage/events.py:41-74`）；`evaluation/replay.py` 从 run.json 重算指标（与事件表无关）；图本体来自 `nodes` 表 | P1（补齐 trace→replay 的链路） | 工程实现（非事件溯源） |
| 9 | trace | 无法回答「这 40 步里哪步错了、花了多少」 | 存储 + 隐私；token 开销 | TRAIL（148 轨迹 / 841 错误，人工标注）；OSWorld/WebArena 的 action 记录 | **有但不闭合**：13 字段 `Tracer`（`observability/tracing.py:19-33`）；**编排成功路径不写 trace**（只有最终 answer 进图） | P0（成功也写 trace） | 工程实现 |
| 10 | budget control | 无限重试/无限上下文烧钱 | 需统一计量口径 | 产业界惯例（无同行评审对照，待核实） | **仅有类**：`Budget`（`policies.py:59-115`）只在测试实例化，生产 `_charge` 是 no-op（`orchestrator.py:361-369`） | P1 | 仅类实现 |
| 11 | human-in-the-loop | 高风险动作需人批 | 延迟与吞吐 | LangGraph `interrupt` 原语（`types.py:92/185`），但**没有审批语义**；τ-bench 的 LM 用户模拟是「人」的替身，不是审批；审批门控无公开 benchmark（待核实） | **无**（PAUSED 状态可人工介入，但无审批语义） | P2 | 待核实 |
| 12 | verifier | 自评不可信 | 需外部判据（测试/状态/规则） | **强证据**：SWE-bench（测试 `swebench/harness/grading.py`）、CORE-Bench（复现结果比对）、τ-bench（DB 终态）、WebArena Verified（后端状态校验） | **有但弱**：Auditor 只在 `audit.refs` 非空时校验引用（`orchestrator.py:237-242`），`known_ids` 不可调用时直接放行（`371-379`） | P0 | 论文机制 |
| 13 | self-correction | 一次做不对 | 多次调用；可能越改越差 | **反向证据强**：ICLR 2024 poster《LLMs Cannot Self-Correct Reasoning Yet》；Self-Refine（NeurIPS 2023）为正面但条件苛刻 | 有「Auditor 打回 → Worker 重跑一次」（`orchestrator.py:252-274`），属有外部判据的修复 | P2（改名 bounded repair，别叫 self-correction） | 论文机制（正反皆有） |
| 14 | bounded repair | 无限修复循环 | 需硬上限 + 终止条件 | Agent 修复预算无专门 benchmark（待核实），但 τ-bench/AgentBench 的多轮上限是同类约束 | **有**：`max_repairs=1`（`state_machine.py:126`），用尽直接 FAILED 并写 `repair_budget_exhausted`（`183-195`）；但 `/orchestrate` 未注入 machine → 生产路径不生效 | P0（一行接线） | 工程实现 |
| 15 | at-least-once / exactly-once | 副作用重复或丢失 | exactly-once 需分布式事务，代价极高 | **Agent 领域无同行评审证据**（待核实）；只有分布式系统理论与框架宣传 | 请求级近似「至多一次 + 结果复用」（`runtime.py:330-348`）；**工具调用无去重**，重试即重复调用 | P1（先做 at-least-once + 幂等键，别宣称 exactly-once） | 仅宣传 / 待核实 |

### 1.1 开源实现里的真实落点（本会话已核到文件路径）

「哪些机制在开源 agent 项目里真的实现了」不能靠印象。本轮实测可核到的落点（GitHub raw 200 + 文件内符号）：

| 机制 | 开源落点（文件:行，均已实取） | 能证明什么 |
|---|---|---|
| checkpoint（状态快照） | `langchain-ai/langgraph` → `libs/checkpoint/langgraph/checkpoint/base/__init__.py:199` `class BaseCheckpointSaver`，含 `get:250 / get_tuple:262 / list:277 / put:301 / put_writes:324 / delete_thread:344` | 框架层确实有「按线程存取检查点」的一等抽象，不只是文档宣传 |
| retry + timeout | 同仓 `libs/langgraph/langgraph/pregel/_retry.py:60` `_timeout_secs`、`:88` `_AttemptContext`、`:112` `_AttemptEvent`、`:312` `on_retry` | 重试是执行器内建路径（含超时解析与重试事件钩子） |
| human-in-the-loop | 同仓 `libs/langgraph/langgraph/types.py:92`（`__all__` 含 `"interrupt"`）、`:185` `interrupts: list[dict]` | HITL 是实现级原语（可在节点处中断并恢复），但**没有审批语义** |
| verifier（执行判据） | `SWE-bench/SWE-bench` → `swebench/harness/grading.py`；`xlang-ai/OSWorld` → `desktop_env/evaluators/metrics/`；`sierra-research/tau-bench` → `tau_bench/envs/base.py:124` `calculate_reward` | 「验证器」在 benchmark 侧是硬代码，不是 LLM 自评 |
| 部分完成计分 | `TheAgentCompany/TheAgentCompany` → `evaluation/run_eval.py` + 加密的 `evaluator.py.enc` | 有 checkpoint 式分档计分，但判分器不公开→复现性打折 |
| 一致性度量 | `sierra-research/tau-bench` → `run.py`（`num_trials`）+ 论文 pass^k | pass^k 有代码入口，但聚合公式在论文而非仓库（脚本层需自实现） |

**没找到落点的**：compensation / rollback（开源 agent 项目里没有通用「撤销已执行副作用」的实现）、exactly-once（无）。这两项只能引分布式系统经典工作，且应写「我们未找到 Agent 领域实现」。

**读表要点（三条）**

- **「类写好了但没接线」是 Atlas 的系统性问题**，不是个别遗漏：`RetryPolicy`、`Budget`、`build_control_flow_graph`、`expire()`、四区 `ContextAssembler` 在测试里全绿，在 `atlas/api/routes.py` 的编排路径上零调用。这意味着**任何「我们实现了 X」的说法都必须附一条生产路径的调用栈**，否则就是框架文档级宣传。
- **有外部判据的可靠性机制才有论文可引**（verifier / partial completion / pass^k / failure taxonomy）；**纯内部机制（idempotency、budget、compensation）只能引工程实践**，写论文时应放在 Implementation 而不是 Contributions。
- **exactly-once 不要碰**。Agent 场景里工具副作用（下单、发邮件）不可撤销，exactly-once 需要两阶段提交 + 业务侧配合，公开文献里没有可信的 Agent 级实现。稳妥表述是「at-least-once 投递 + 幂等键 + 可观测的重复率」。

---

## 2. 逐 benchmark 分析（统一 15 字段）

字段固定为：①全称 ②领域/来源 ③任务数 ④年份·venue ⑤判分方式 ⑥判分代码路径 ⑦判定粒度 ⑧多轮/有状态 ⑨环境重置 ⑩可复现性/许可 ⑪官方榜/公开分数 ⑫对 Atlas 的用途 ⑬本次真实证据 ⑭成本 ⑮可信度（+待核实）。

### 2.1 SWE-bench（含 Lite / Verified）

① SWE-bench: Can Language Models Resolve Real-World GitHub Issues? ② 真实 GitHub issue + 对应 PR，12 个 Python 仓库 ③ 2,294（论文摘要原文）；Lite **300**、Verified **500** ④ 2023/2024，**ICLR 2024**（OpenReview `venue=ICLR 2024 oral`；自核）⑤ 打 patch 后跑测试：FAIL_TO_PASS 必须转通过、PASS_TO_PASS 不得回归 ⑥ `swebench/harness/grading.py`（raw 200，实取到文件头）+ `run_evaluation.py` ⑦ 二值（resolved / not）⑧ 单轮：一次提交一个 patch，无跨任务状态 ⑨ Docker 镜像按仓库构建，完全可重置 ⑩ MIT；Docker 全容器化；数据在 HF ⑪ 公开 leaderboard ⑫ **验证器范式**的参照物；不建议 Atlas 直接跑（成本高、与记忆无关）⑬ GitHub API：`SWE-bench/SWE-bench` MIT / 6141 stars；`contents/swebench/harness` 列出 `grading.py`；HF datasets-server：Lite test=300、Verified test=500 ⑭ 高（每任务一次 Docker 构建 + 测试）⑮ **高**（判分是执行结果，最难作弊）。

### 2.2 WebArena / WebArena Verified

① WebArena: A Realistic Web Environment for Building Autonomous Agents ② 四个自托管站点（电商/论坛/GitLab/CMS）+ 工具 + 知识库 ③ **812**（README 原文「evaluating the 812 examples」）④ 2023/2024，**ICLR 2024 poster**（Lead 核验表 / OpenReview `ICLR 2024 poster`）⑤ 功能正确性：任务特定的检查器（URL、页面内容、后端状态）⑥ `evaluation_harness/evaluators.py` + `helper_functions.py`（tree 列表 + raw 200 实取）⑦ 二值 ⑧ **多轮、有状态**（站点数据会被改）⑨ 需按 `environment_docker/README.md` 重置 ⑩ Apache-2.0（GitHub API 实取）⑪ 公开榜 ⑫ 长程 web 任务的参照；判分脆弱性的反面教材 ⑬ GitHub tree/raw + README ⑭ 中高（要起 4 个站点）⑮ **高**（但对判分器信任度：见下条）。

**WebArena Verified: Reliable Evaluation for Web Agents**（新增，与评测可靠性直接相关）① 同名 ② 对 WebArena 全部 812 任务的重审 ③ 812 ④ **SEA @ NeurIPS 2025 Poster**（OpenReview `venue=SEA @ NeurIPS 2025 Poster`，自核；注意另有 `ICLR 2026 Conference Withdrawn Submission` 记录，**不可当录用**）⑤ 摘要原文：保留容器环境，重写检查器为「类型与归一化感知比较器」，对状态变更任务**校验后端状态**，采用带显式状态码的 JSON schema 做确定性判分，报告**模板级宏平均 + 95% 置信区间** ⑥ 代码未取到（待核实）⑦ 二值 + 分档 ⑧ 多轮有状态 ⑨ 同 WebArena ⑩ 待核实 ⑪ 无 ⑫ **这是 Atlas 修 probe 的最直接依据**：不要子串匹配、要查状态、要报区间 ⑬ OpenReview notes/search 原文摘要 ⑭ 低（读方法即可）⑮ **高**（venue 明确）/ 方法细节待核实。

### 2.3 OSWorld

① OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments ② 真实操作系统（Ubuntu/Windows/macOS）+ 真实应用 ③ **369**（摘要原文）④ 2024，**NeurIPS 2024**（Lead 表 / OpenReview `NeurIPS 2024 Track Datasets and Benchmarks Poster`）⑤ 每个任务自带 execution-based 评测脚本，比对文件/应用状态 ⑥ `desktop_env/evaluators/getters/`、`desktop_env/evaluators/metrics/`（tree + raw 200：`metrics/table.py`）⑦ 二值/按脚本 ⑧ 多轮有状态 ⑨ VM 快照 ⑩ Apache-2.0（API 实取）；VMware/Modal/AWS provider ⑪ 官方榜 ⑫ 「环境状态一致性」的最佳参照 ⑬ README 2025-07-28 条目：**OSWorld-Verified** 已发布 ⑭ 高（VM/云主机）⑮ **高**。

### 2.4 τ-bench

① τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains ② 客服域（retail/airline），LM 模拟用户 + 域内 API + 政策文档 ③ **retail 115 / airline 50**（论文 Table 1 原文：Tasks 115 | 50；DB 500 users/50 products/1000 orders 与 500/300/2000）④ 2024/2025，**ICLR 2025 poster**（Lead 核验表；本人 OpenReview 二次检索未命中标题格式化记录）⑤ **对话结束时数据库状态 vs 标注目标状态** ⑥ `tau_bench/envs/base.py::calculate_reward`（raw 实取，124 行起）⑦ 二值（reward 1/0）+ 聚合 pass^k ⑧ 多轮有状态 ⑨ 每 episode 重置 DB ⑩ MIT（API 实取）；README 提示已迁往 τ²/τ³-bench ⑪ 论文数字 + 仓库表格 ⑫ **pass^k 与规则遵守率**的直接来源 ⑬ 论文 HTML 正文（Table 1、pass^k 定义、temperature 设定）+ 仓库 README/tree ⑭ 低（纯 API 调用，无容器）⑮ **高**（判分查状态而非文本）。

### 2.5 AgentBench

① AgentBench: Evaluating LLMs as Agents ② 8 个环境（OS、DB、知识图谱、卡牌、家居、网购、网页等；其中 5 个为本文新建）③ **8 个环境**；单环境规模不等（如 OS 交互 1,663 题，论文正文）④ 2023/2024，**ICLR 2024 poster**（自核 OpenReview + Lead 表）⑤ 环境特定成功率，汇总为总分 ⑥ `src/server/tasks/{os_interaction,alfworld,webshop,dbbench,knowledgegraph}`（tree 实取）⑦ 二值/环境分 ⑧ 多轮有状态 ⑨ 每环境 Docker ⑩ Apache-2.0（LICENSE 原文实取）⑪ 公开榜 ⑫ 多环境泛化的老牌基线 ⑬ GitHub tree + LICENSE + 论文 HTML ⑭ 中 ⑮ **中高**（判分依赖各环境实现，未逐一读）。

### 2.6 GAIA

① GAIA: a benchmark for General AI Assistants ② 真实世界问答，需浏览/工具/多模态 ③ **466**（其中 166 dev 带标注、**300 测试集不公开答案**）④ 2023/2024，**ICLR 2024 poster**（自核）⑤ **quasi exact match**：答案与金标在「按类型归一化」后比对；论文原文「We provide our scoring function along with the leaderboard」⑥ 官方评分函数随榜发布，**独立仓库未取到**（待核实）⑦ 二值 ⑧ 单轮（一问一答，可多次工具调用）⑨ 无环境 ⑩ 数据集 HF（gated，本轮 datasets-server 未返回规模 → 待核实）⑪ 公开榜 ⑫ 不适合 Atlas（无状态、无恢复）；只作能力对照 ⑬ 论文 HTML 全文关键句 ⑭ 低 ⑮ **高**（论文）/ **中**（自建复现判分器需自行实现）。

### 2.7 LongMemEval

① LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory ② 长期交互记忆，五项能力：信息抽取、多会话推理、时序推理、**知识更新（KU）**、**弃答（ABS）** ③ **500**（README 与论文均写 500；每文件 500 实例）④ 2024/2025，**ICLR 2025**（Lead 表；arXiv Comments「ICLR 2025」）⑤ LLM-as-judge 判分 ⑥ `src/evaluation/evaluate_qa.py` + `print_qa_metrics.py`（raw 200 实取）；README 给出 `python3 evaluate_qa.py gpt-4o <hyp> ../../data/longmemeval_oracle.json` ⑦ 二值（正确/错误）+ 分能力均值 ⑧ 多会话历史，**有状态（记忆）** ⑨ 每次独立 ⑩ 许可见 README（待核实具体 SPDX）⑪ 论文表格 ⑫ **Atlas 最该接的公开基准**：与「图状态记忆」正面相关，且 KU 子集直接对应「过期信息误用」⑬ README + raw 文件 + 论文 HTML（500 题、五能力）⑭ 低（长上下文 API 调用）⑮ **高**。

### 2.8 TRAIL

① TRAIL: Trace Reasoning and Agentic Issue Localization ② Agent 轨迹的错误定位，跨 GAIA / SWE-bench-Lite ③ **148 条人工标注轨迹 / 841 个唯一错误**（论文 HTML 原文）④ 2025，**arXiv preprint**（arXiv Comments 只给数据集链接）⑤ 在轨迹上定位错误 span（层次化 taxonomy）⑥ 代码仓库**未取到**（`pat-jj/TRAIL` 404；数据集在 HF `PatronusAI/TRAIL`）⑦ span 级 ⑧ 轨迹级 ⑨ 无 ⑩ 数据集公开；代码许可待核实 ⑪ 论文：最好的 gemini-2.5-pro 仅 **11%** ⑫ **错误分类学的直接来源**（Atlas 的 failure taxonomy 应映射到它）⑬ 论文 HTML（三层九类）+ arXiv Comments（数据集 URL）⑭ 低 ⑮ **高**（数据集）/ 中（判分脚本细节待核实）。

### 2.9 AgentBoard

① AgentBoard: An Analytical Evaluation Board of Multi-turn LLM Agents ② 9 个任务、**1,013 个环境**（论文原文）③ 1,013 ④ 2024，**NeurIPS 2024 (Oral)**（arXiv Comments 原文「NeurIPS 2024 (Oral)」自核 + Lead 表）⑤ **progress rate（0–1 连续）** + grounding accuracy + 子技能分解 ⑥ `agentboard/eval_main.py`（tree 实取）⑦ 连续 ⑧ 多轮有状态 ⑨ 环境级 ⑩ 代码 Apache-2.0 / 数据 GPL-2.0（README badges；LICENSE 文件 404 → 待核实）⑪ 论文表 ⑫ **中间步骤成功率**与「计划偏差」的最接近口径 ⑬ 论文 HTML + tree + README ⑭ 中 ⑮ **高**。

### 2.10 CORE-Bench

① CORE-Bench: Fostering the Credibility of Published Research Through a Computational Reproducibility Agent Benchmark ② 计算可复现性（CS/社科/医学，Python/R）③ **270 个任务 / 90 篇论文**（README 原文）④ 2024，**arXiv preprint（会议归属待核实）**（arXiv Comments 只有代码链接）⑤ 按任务问题（task questions）判定是否复现出论文结果 ⑥ `main.py` + `benchmark/evaluations.py`（tree 实取）⑦ 分档（Easy/Medium/Hard）⑧ 单轮长任务（安装依赖、跑代码、读结果）⑨ Docker per agent-task ⑩ **MIT**（LICENSE 原文实取）⑪ 论文表 ⑫ **「验证器 = 真实执行」**的第二个范例；也示范了「隔离环境」的必要性 ⑬ GitHub tree + LICENSE + README ⑭ 高（Docker/VM）⑮ **高**。

### 2.11 TheAgentCompany

① TheAgentCompany: Benchmarking LLM Agents on Consequential Real World Tasks ② 模拟软件公司（GitLab/Plane/RocketChat/ownCloud）③ **175** 个任务 ④ 2024，**arXiv preprint**（Comments「Preprint」自核）⑤ **checkpoint 式部分给分**（论文正文「Checkpoint 1 (5pts)」）+ 结果型评测 ⑥ `evaluation/run_eval.py`；**`evaluator.py.enc`（加密）** ⑦ 分档（checkpoint 累加）⑧ 多轮有状态 ⑨ 整套服务栈快照 ⑩ **MIT**（LICENSE 原文实取）；**判分器加密 → 外部无法完全复算** ⑪ 论文表（最佳 agent 自主完成 24%）⑫ **partial completion 的唯一权威范例**；同时是「可复现性打折」的活例子 ⑬ README + 论文 HTML + raw 文件 ⑭ 高 ⑮ **高**（事实）/ **中**（复现性）。

### 2.12 RE-Bench

① RE-Bench: Evaluating frontier AI R&D capabilities of language model agents against human experts ② 7 个 ML 研究工程任务 ③ **7**（仓库 7 个 `ai_rd_*` 目录实取：fix_embedding / nanogpt_chat_rl / optimize_llm_foundry / restricted_mlm / rust_codecontests_inference / small_scaling_law / triton_cumsum）④ 2024，venue 待核实（arXiv 2411.15114 由仓库 CITATION 给出）⑤ 与人类专家基线做归一化比较 ⑥ `suite_manifest.yaml` + METR Task Standard ⑦ 连续（归一化分数）⑧ 长任务、单轮 ⑨ 任务级容器 ⑩ **MIT**；**README 首行标注 DEPRECATED（不再接受贡献/bug 报告）** ⑪ METR 报告 ⑫ 「人类基线归一化」的报告方式可借鉴 ⑬ README + tree + CITATION ⑭ 很高（GPU）⑮ **中**（已弃用，复现风险高）。

### 2.13 选型结论（给 Atlas）

- **能直接接、成本最低**：LongMemEval（500 题、官方 judge、与记忆强相关）> τ-bench retail（115 题、判分查 DB 状态、MIT）> AgentBoard 子集（连续 progress rate 可做中间指标）。
- **只能借鉴方法、不要跑**：SWE-bench（成本/无关）、OSWorld（VM 重）、TheAgentCompany（判分器加密）、RE-Bench（已弃用 + GPU）、GAIA（无状态）、CORE-Bench（Docker 重）。
- **必须引用、但别当自己成绩**：WebArena Verified（判分方法学）、TRAIL（错误分类学）、MAST（多智能体失败分类）。

---

## 3. 指标可操作化定义

统一记法：一个 **run** = 一个任务在一个配置下的一次完整执行；一个 **arm** = 一种方法（Atlas 图状态 / 全历史 / 摘要基线）；分母一律写清。

### 3.1 任务最终成功率（task success rate, SR）

- 分子：满足**全部**成功判据的 run 数；分母：计划执行的 run 数（含失败的，不含基础设施错误——后者单列 `infra_failure_rate`，参照 SWE-bench 的 infra 分类思路）。
- 判定规则：必须是**外部判据**（测试通过 / DB 终态相等 / 环境状态断言），不能是「答案包含某个字符串」。SWE-bench 的 FAIL_TO_PASS+PASS_TO_PASS 是范本；τ-bench 的「DB 状态 == 目标状态」是第二范本。
- 报告：点估计 + Wilson 95% CI；同时报 `n`。

### 3.2 中间步骤成功率（step success rate）

- 分子：通过**步骤级校验**的步数；分母：该 run 计划步数（不是实际执行步数，否则跳过步骤会白赚）。
- 判据来源：有子目标标注的用 AgentBoard 的 progress rate（连续，按子目标完成比例）；有 checkpoints 的用 TheAgentCompany 的 checkpoint 给分；两者都没有就**不要报这个指标**（自造子目标 = 循环论证）。
- 对 Atlas：`StepResult`（`evaluation/harness.py:28-47`）已有结构，但目前只在离线 fixture 用，且 probe 是子串匹配——必须换成结构化断言后才能进表。

### 3.3 约束遵守率（constraint adherence rate）

- 分子：**全程零违规**的 run 数；分母：全部 run 数。
- 判定：违规必须由**规则**判定（正则/结构化检查），不能由同一个模型自评。τ-bench 的域政策（retail/airline policy）是标准做法：政策写成自然语言 + 可判定的规则清单。
- Atlas 现状：`metrics.py:37` 的 `constraint_violation_rate` 是「任一 step 命中 forbidden_method 子串即 1」——方向对，判据太粗（同义词/改写会漏）。

### 3.4 计划偏差率（plan deviation rate）

- 定义（建议）：`plan_deviation = 1 − (计划步与执行步的最优对齐得分)`。实现：把计划的 step 序列与执行的 step 序列做编辑距离对齐（插入/删除/替换），`1 − LCS/len(plan)`。
- 分母：计划步数；分子：未被执行或被替换的计划步数 + 计划外新增步数（各计 1，可加权）。
- 依据：AgentBoard 的 progress rate 提供「子目标完成比例」，TRAIL 的 **Task Management**（计划协调错误）类目提供「计划与执行不一致」的错误归属；但目前**没有公认的计划偏差指标**——这是待核实项，若要用必须在论文里给完整定义并开源实现。
- 注意：偏差率**不是越低越好**（动态重规划会提高偏差但可能是正确行为），必须与 SR 联合报告。

### 3.5 恢复成功率（recovery success rate）★ 非标准指标

这是 Atlas 最有差异化、也最容易自欺的指标。可落地定义：

1. **样本单位**：一次「故障注入事件」（fault injection event），不是一次 run。一个 run 可以有多个注入点，但**每个注入点单独记一个样本**（否则长任务被稀释）。
2. **故障类型**（必须分类，参照 TRAIL 的 System Execution 三层）：①工具瞬时失败（timeout / 5xx / 429）；②工具永久失败（no_evidence / 404）；③进程级中断（kill -9 后重启）；④上下文超限（组装溢出）；⑤上游模型错误输出（非法 JSON）。
3. **「成功恢复」判定（三选三，缺一不可）**：
   - a) **任务层**：注入后 run 仍然达到最终成功判据（同 3.1）；
   - b) **状态层**：恢复后的图形/DB 状态与「无故障对照 run」在关键节点集合上一致（节点存在性 + 状态字段 + 边），即**没有丢步、没有重复副作用**；
   - c) **成本层**：恢复所消耗的额外 token/调用不超过阈値（建议 ≤ 失败前已消耗的 50%），否则算「恢复但代价不可接受」，单列 `recovered_degraded`。
4. **分母**：**实际触发**的注入事件数。若某次注入没有真正生效（例如工具本来就没被调用），该样本**必须剔除**，不能记 0 也不能记 1。
5. **无样本时**：报 `None`，不报 1.0。Atlas 现在的 `metrics.py:39-41` 在 `interrupted` 为空时返回 1.0——这正是审计点名的「自证 100%」。

依据说明：**没有公开 benchmark 定义过这个指标**（待核实范围内），最接近的是 ①SWE-bench 的 infra failure 与 resolved 分离、②TheAgentCompany 的 checkpoint 部分给分、③OSWorld/WebArena 的「环境重置后状态一致」。因此 Atlas 若提出这个指标，属于**自创指标**，必须：给完整可实现定义 + 开源注入器 + 报告注入真的生效的比例（`injection_hit_rate`），否则外人看来就是「自己造故障自己判恢复」。

### 3.6 过期信息误用率（stale information misuse rate）★ 非标准指标

这是 Atlas 最需要的指标，也是现有实现最宽的一个（`metrics.py:38` + `harness.py:223-226`：答案里出现 `stale_value` 且不出现目标值即算误用）。

**可落地定义（两档，建议都报）**

- **档 A：决策级（严格，主指标）**
  - 样本单位：**存在「值被更新」的决策点**的任务。构造方式：任务历史中，某事实 F 在 t1 取 v1，在 t2 被更新为 v2（Atlas 里对应 `supersede` / `expire` / `valid_until` 过期，`memory/schema.py:34-38`、`context/provenance.py:48-59`）。
  - 分子：**下游动作/结论以 v1 为依据**的任务数——判定不看文本是否「提到」v1，而看**行为**：写库字段、调用参数、最终答案的断言值等于 v1 且不等于 v2。
  - 分母：**该决策点被实际触达**的任务数（即任务确实需要这个事实；没触达就不进分母）。
  - 判定规则：`misuse = (asserted_value == v1) AND (v2 is known in history) AND (task_requires_F)`。三元条件缺一不可。
  - 依据：LongMemEval 的 **knowledge update (KU)** 能力项就是这个口径（用户信息在会话间变化，判分看是否用最新值）；τ-bench 的政策遵守同理（用旧政策即违规）。
- **档 B：表述级（宽，辅助指标）**
  - 分子：最终答案文本中出现了任何 `expired/superseded` 节点的值，且未同时给出正确的现值；分母：全部含过期事实的任务。
  - 这是 Atlas 现在的口径，**只能作为上界的粗筛**，不能单独作为结论。
- 另报两个配套量，防止指标被玩坏：`stale_exposure_rate`（模型实际看到过期值的比例，衡量检索器是否把过期内容送进上下文）与 `correction_rate`（看到过期值后主动纠正的比例，= 1 − 误用率/暴露率）。

### 3.7 工具调用正确率

- 分子：`tool_name` 与参数 schema 都正确、且**副作用符合预期**的调用数；分母：全部工具调用数。
- 判据优先级：①参数 schema 校验（可自动）→ ②环境状态 diff（τ-bench 式）→ ③人工/LLM rubric（最弱，需报与人工的一致性 κ）。
- 错误归类直接借 TRAIL：API/系统错误、配置错误、资源管理（这三类正好覆盖工具层）。
- Atlas 现状：`tools/protocol.py` 的五类错误可作为**失败分类**，但没有「调用是否正确」的判据（只有失败与否）。

### 3.8 环境状态一致性

- 分子：终态与期望状态在**结构化断言**下相等的任务数；分母：所有会改状态的任务数。
- 判据：文件哈希/DB 行/配置项，而不是截图或自然语言。OSWorld 的 per-task 评测脚本、WebArena Verified 的「state-changing tasks 查后端状态」是直接范本。
- 对 Atlas：图记忆的「状态一致性」应定义为 `nodes/edges` 与事件表 replay 结果一致（当前 `import_engine` 会跳过已存在节点，导致生命周期变更不进事件表——`repository.py:469-471`，这是真实的一致性漏洞）。

### 3.9 成本 / 延迟 / token

- 必须**实测**，禁止合成。Atlas `harness.py:422` 的 `latency_ms = context_tokens*0.5 + model_calls*8` 是编造值（`latency_source="synthetic_estimate"`），任何表格里出现它都必须标注或被删除。
- 成本口径：分开报 **prompt tokens / completion tokens / cached tokens**，单价单列成表（因为单价会变），不要直接报美元。
- 延迟口径：报 p50 / p95，并标注是否含工具时间。

---

## 4. 方差、重复运行与显著性

### 4.1 τ-bench 的 pass^k：原文定义（已核实）

论文正文（HTML 版 §3）原文：pass@k 是「k 次独立试验中至少一次成功」的概率；**pass^k（pass hat k）定义为「k 次独立试验全部成功」的概率，并在任务上取平均**。给定每任务 n 次试验、其中 c 次成功，无偏估计为：

```
pass^k = E_task[ C(c, k) / C(n, k) ]
pass@k = 1 − E_task[ C(n−c, k) / C(n, k) ]
```

要点：**k ≤ n**；n=8、k=8 时 pass^8 就是「8 次全对的任务比例」；k=1 时两者都退化为平均成功率。原文实验：**agent temperature = 0.0，user simulator temperature = 1.0，每任务至少 3 次试验**；gpt-4o 在 τ-retail pass^1 ≈ 61%、pass^8 < 25%。

### 4.2 跑几次

- **报告一致性（pass^k）**：n ≥ 8（τ-bench 用 8 作为展示上限），k 取 1/2/4/8 报曲线。n 太小（如 2）时 pass^2 的方差极大，几乎不可解释。
- **报告方法差异（A/B）**：成对设计——同一任务、同一 seed 集合跑两个 arm，用 **McNemar 检验**（只统计 discordant 对）。成对设计的样本效率通常比独立两臂高 3–5 倍（本报告算术，非常数结论）。
- **功效估算**：独立两臂、基线 60% vs 70%、α=0.05、power=0.8，每组约需 **356 个任务**（本报告按两比例公式算术）。这条直接判了 Atlas 现状的死刑：**5 个自建场景不可能支撑任何效果结论**，只能做机制演示。

### 4.3 温度与采样

- **temperature=0 只能证明「基础设施可重复」**，不能证明鲁棒。Atlas 的 40 步实验重复 2 次、两次完全一致，正确的解读是「没有非确定性抖动」，不是「方法稳」。
- 建议三档报告：①T=0、n=2（可复现性）；②T=1、n=8（鲁棒性，报 pass^k）；③T=1、n=8、换 seed（seed 敏感性）。
- **必须固定并公开**：模型名与版本、`top_p`、`max_tokens`、系统提示、工具实现版本、判分器版本。Atlas 目前 `atlas/` 内 grep 无任何 temperature/seed 设置（审计 §3），这本身就是一个可复现性缺陷。
- user simulator / judge 的温度要单列——τ-bench 把两者分开（agent 0.0 / user 1.0）就是承认「用户模拟的随机性是任务难度的一部分」。

### 4.4 报告区间的方式

- 比例类：**Wilson 区间**（不是正态近似，n 小或 p 接近 0/1 时正态近似会越界）。示例：40/40 → 95% CI ≈ [0.912, 1.000]（算术）。
- 配对差异：报「差值 + bootstrap 95% CI（按任务重采样）」+ McNemar p 值。
- 连续指标（token、延迟）：报中位数与 IQR，或按任务聚类的 bootstrap 区间；**不要报均值 ± 标准差**（长尾会骗人）。
- 多重比较：多 arm 多指标时要么预注册主指标（primary endpoint），要么 Holm 校正；否则「三个基线里赢两个」是挑樱桃。
- **明确写 n 与分母**：`1.000 (40/40)` 比 `1.000` 诚实得多。

---

## 5. Atlas 评测改造清单（按成本排序 + 循环论证怎么破）

### 5.1 按成本/可信度排序

| 序 | 动作 | 成本 | 让外人相信什么 | **不能**让外人相信什么 |
|---|---|---|---|---|
| 1 | **修判分口径 bug**：`contains_probe` 子串匹配改结构化断言；`recovery_success_rate` 无样本返回 `None`；`revision_ok` 校验修订稿内容；`coverage_ok` 换独立金标；删掉合成延迟 | 极低（1–2 天） | 内部自洽、指标定义清楚 | 任何效果数字 |
| 2 | **预注册 + 冻结**：把指标定义、n、seed、判分器版本写进 `docs/eval-protocol.md`，打 git tag；原始 trace/prompt 全量落盘 | 低（2–3 天） | 没有事后挑指标（HARKing） | 方法优越性 |
| 3 | **head-to-head（同条件）**：同模型、同提示、同预算，Atlas vs long_context vs MAGE vs 无记忆；成对 McNemar | 低–中（1 周） | 差异不是配置差异造成的 | 对外泛化（仍自建任务） |
| 4 | **接 LongMemEval**：500 题、官方 `evaluate_qa.py`、报五能力分 + KU 子集误用率 | 中（1–2 周，含复现官方 judge） | 在公开数据集上不输/优于基线 | 端到端 agent 能力 |
| 5 | **接 τ-bench retail 子集**：115 题、DB 终态判分、pass^k（n=8） | 中（2 周） | 规则遵守 + 一致性有公开口径 | 成本优势（τ-bench 不吃长上下文） |
| 6 | **故障注入协议 + 恢复成功率**：五类故障、三条件判定、开源注入器、报 `injection_hit_rate` | 中–高（3–4 周） | 唯一能证明「恢复」这一主张的东西 | 与 SOTA 的比较（无公开基线） |
| 7 | **公开复现包**：Docker + 一键脚本 + 失败案例集（含 negative results） | 高（1 个月） | 结果可被别人跑出来 | 结论正确性 |
| 8 | **第三方复现 / 提交官方榜** | 最高 | 社区认可 | — |

**排序理由**：1–2 是「让内部数字不作废」的止损；3–5 才是「让外人信」的起点；6 是 Atlas 的差异化贡献（但只有前 5 步做完才有意义）；7–8 是发表级要求。

### 5.2 哪些自评方式在外人看来是循环论证

1. **自建 fixture + 自写 probe**：语料、期望 chunk、判据都由作者写，检索器按同一 ranking 返回 → `coverage_ok` 必然成立（`merged_e2e.py:118-121,254`）。外人会直接说「你测的是你的 fixture」。
2. **脚本替身扮演「研究者」**：`integration/merged_e2e.py` 的 LLM 全是脚本替身，判分与生成同源；即便改成「只依据上下文作答」（PROJECT.md 续九已做），仍是作者设定规则。
3. **同一模型既当选手又当裁判**：LongMemEval 允许 LLM judge，但必须用**固定的第三方模型 + 官方脚本**，且报 judge 与人工的一致性；Atlas 若自写 judge 就是闭环。
4. **「我们的机制提升了 X」但基线被削弱**：摘要基线只留 2 条摘要（PROJECT.md 自陈），完成率 0.000 是设计使然，不是能力差异——这类对比必须在论文里明写「基线为下界」。
5. **恢复成功率无样本记 1.0**：等于「没做过实验宣称 100%」。
6. **延迟/成本是估算值**：`synthetic_estimate` 一旦进表，整张表都会被质疑。

### 5.3 怎么破（五条，按性价比）

- **换数据不换方法**：把同一套 Atlas 记忆系统接到 LongMemEval / τ-bench 上，判分器用官方脚本。**这是唯一低成本的破法**——方法不用改，判据不再由作者写。
- **预注册**：在跑之前把「主指标 + n + seed + 判据」写进仓库并打 tag，事后只报预注册的指标；探索性指标单列附录。
- **判分器与生成器分离**：judge 用固定版本第三方模型 + 官方 prompt；报 judge-human κ（抽 50 条人工双盲）。
- **head-to-head 同预算**：同 prompt、同 token 预算、同工具集、同模型，报成对差异 + CI；不做「不同预算下比较」。
- **公开失败案例**：把失败 trace 与分类（映射到 TRAIL 三层九类）一并开源；审稿人对「愿意展示失败」的系统信任度显著更高（这是判断，非实证结论）。

### 5.4 与 §1 的联动（机制主张怎么写才不被戳）

- 只能说「我们实现了 checkpoint / 幂等 / 有界修复」，**不能**说「这些机制提升了可靠性」——除非有 §4 的成对实验。
- 若要把「恢复」当贡献，必须给：注入器 + 三条件判定 + 无故障对照 + 注入命中率。
- 「exactly-once」删掉，改「at-least-once + 幂等键 + 重复副作用可观测」。

---

## 6. 元数据核验表

| 简称 | 标题 | 年份 | venue（工具原样） | 链接 | 核验方式 |
|---|---|---|---|---|---|
| SWE-bench | SWE-bench: Can Language Models Resolve Real-World GitHub Issues? | 2023/2024 | ICLR 2024（OpenReview 原文 `ICLR 2024 oral`） | https://arxiv.org/abs/2310.06770 | OpenReview notes/search（自核）+ arXiv abs |
| WebArena | WebArena: A Realistic Web Environment for Building Autonomous Agents | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2307.13854 | OpenReview notes/search「ICLR 2024 poster」+ Lead 表 |
| WebArena Verified | WebArena Verified: Reliable Evaluation for Web Agents | 2025 | SEA @ NeurIPS 2025 Poster | https://openreview.net/forum?id=94tlGxmqkN | OpenReview notes/search 原文 `venue=SEA @ NeurIPS 2025 Poster`（另有 ICLR 2026 Withdrawn，不计） |
| OSWorld | OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments | 2024 | NeurIPS 2024（OpenReview `NeurIPS 2024 Track Datasets and Benchmarks Poster`） | https://arxiv.org/abs/2404.07972 | OpenReview 自核 + 仓库 README |
| τ-bench | τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains | 2024/2025 | ICLR 2025（poster） | https://arxiv.org/abs/2406.12045 | Lead 核验表（本人二次检索未命中格式化的标题记录） |
| AgentBench | AgentBench: Evaluating LLMs as Agents | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2308.03688 | OpenReview 自核 `ICLR 2024 poster` + Lead 表 |
| GAIA | GAIA: a benchmark for General AI Assistants | 2023/2024 | ICLR 2024（poster） | https://arxiv.org/abs/2311.12983 | OpenReview 自核 `ICLR 2024 poster` |
| LongMemEval | LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory | 2024/2025 | ICLR 2025 | https://arxiv.org/abs/2410.10813 | Lead 表（arXiv Comments「ICLR 2025」） |
| TRAIL | TRAIL: Trace Reasoning and Agentic Issue Localization | 2025 | arXiv preprint（Comments 仅给数据集链接） | https://arxiv.org/abs/2505.08638 | arXiv abs Comments 自核 |
| AgentBoard | AgentBoard: An Analytical Evaluation Board of Multi-turn LLM Agents | 2024 | NeurIPS 2024（Oral） | https://arxiv.org/abs/2401.13178 | arXiv abs Comments 自核「NeurIPS 2024 (Oral)」 |
| CORE-Bench | CORE-Bench: Fostering the Credibility of Published Research Through a Computational Reproducibility Agent Benchmark | 2024 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2409.11363 | arXiv abs Comments 仅代码链接（自核） |
| TheAgentCompany | TheAgentCompany: Benchmarking LLM Agents on Consequential Real World Tasks | 2024 | arXiv preprint（Comments「Preprint」） | https://arxiv.org/abs/2412.14161 | arXiv abs Comments 自核 |
| RE-Bench | RE-Bench: Evaluating frontier AI R&D capabilities of language model agents against human experts | 2024 | 待核实（仓库 README 首行标注 DEPRECATED） | https://arxiv.org/abs/2411.15114 | 仓库 `CITATION` 块 + README 自核；venue 未取到 |
| MAST | Why Do Multi-Agent LLM Systems Fail? | 2025 | NeurIPS 2025 Datasets and Benchmarks Track（spotlight） | https://openreview.net/forum?id=fAjbYBmonr | OpenReview notes/search 自核（另有 LLM Evaluation Workshop Poster 记录） |
| Self-Correct | Large Language Models Cannot Self-Correct Reasoning Yet | 2023/2024 | ICLR 2024（poster） | https://openreview.net/forum?id=IkmD3fKBPQ | OpenReview notes/search 自核 |
| Self-Refine | Self-Refine: Iterative Refinement with Self-Feedback | 2023 | NeurIPS 2023 | https://openreview.net/forum?id=43rnkOcpI1 | OpenReview notes/search 自核 |

### 6.1 本轮实测的核验渠道可用性（供后续复用）

- ✅ 可用：GitHub `raw.githubusercontent.com`（**需代理 `-x http://127.0.0.1:7897`**，直连会 000/超时）、GitHub HTML tree 页（同需代理，可抓 `"name":"…"` 列目录）、arXiv abs/html（直连快）、OpenReview `api2.openreview.net/notes/search`、HF `datasets-server.huggingface.co/size`（需代理）。
- ❌ 不可用：GitHub **API** `api.github.com`（配额 0/60，共享 IP 已耗尽；只能靠 raw + HTML 替代）、dblp（Anubis 反爬）、OpenAlex/Crossref/Semantic Scholar（429）、arxiv.org 检索页（400）。
- ⚠️ 注意：`paper_search` 的 venue 列与 `paper_cite` 一律回 arXiv/CoRR，**不能**用于会议归属；本轮多次 paper_search 因上游 429 返回无关结果，元数据最终以 OpenReview/arXiv Comments/仓库为准。

### 6.2 待核实清单（本报告内显式标注的）

1. τ-bench 的 ICLR 2025 归属：本人在 OpenReview 二次检索未命中（标题含 `$τ$` 格式化），暂信 Lead 核验表。
2. GAIA 官方评分函数是否作为独立代码发布（论文称「随 leaderboard 提供」，本轮未取到仓库）。
3. GAIA 在 HF 的 gated 数据集实际规模（datasets-server 未返回，466 来自论文原文）。
4. TRAIL 的**代码仓库**（`pat-jj/TRAIL` 404）与判分脚本；数据集 `PatronusAI/TRAIL` 已确认存在。
5. CORE-Bench 的会议归属（Comments 无会议信息）；`benchmark/evaluations.py` 的判分细节未逐行读。
6. RE-Bench 的 venue 与已弃用状态对复现的影响。
7. TheAgentCompany `evaluator.py.enc` 的解密与复算条件（README 未说明是否公开可解）。
8. AgentBoard 的 LICENSE 具体类型（仓库根 `LICENSE` 404，README badge 标 Apache-2.0 代码 / GPL-2.0 数据）。
9. 所有「Agent 领域无同行评审证据」的判断（idempotency / compensation / budget / HITL / exactly-once）属于**检索结论**，非定论——若审稿人知道反例，应在正文改口径为「我们未找到」。
10. 计划偏差率无公认实现，本文给的是自拟定义。

---

**边界声明**：本文只覆盖公开 benchmark 与论文/仓库层面的事实核验与 Atlas 代码层面的机制对应；未运行任何 benchmark，未修改 Atlas 代码。Atlas 的 file:line 事实与 `notes/research-2026-11/atlas-audit.md` 一致，其中 §1 表中的关键行号（`state_machine.py:23-55`、`runtime.py:124/153/180`、`metrics.py:36-41`、`harness.py:107-114`、`policies.py:59-115`、`tools/protocol.py:13-22`）本人已直接读源码复核。
