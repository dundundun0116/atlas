# CompassRAG 升级实施计划（双项目路线图 · 单项抽取版）

> **来源与范围。** 抽取自 `2026-10-28-agent-internship-two-project-roadmap.md`（2026-10-28）。只保留 CompassRAG 相关部分：Atlas 的阶段 4/5/6、Atlas 技术栈、Atlas 面试题已剔除；双项目内容只保留一句边界判据、阶段 0 的共用事件字段、阶段 7 的共用 trace 约定。
>
> **标注约定。** 不加标记的文字来自原文档（措辞基本保留）；`〔修正〕` 标出与原文档不同之处并给理由；`现状（实测）` 引用块是 2026-10-28 读代码、读运行产物补的现状，**不属于原计划**，但直接决定哪些项能开工、哪些项的成本被低估。
>
> **执行要求。** 按阶段逐项执行，每项交付都要有测试或实验产物；未拿到实验结果的数字不得写进简历。

## 0. 一页速览

| 阶段 | 主题 | 关键产出 | 验收关键词 | 现状（实测，2026-10-28） |
|---|---|---|---|---|
| 0 | CompassRAG 侧边界与接口 | 架构图、统一事件字段 | 一句话讲清"问题内搜索" | 架构图已有；五个跨项目事件字段缺 |
| 1 | 工具调用化 | `agent/schemas.py`、`tools.py`、`state.py` | 格式错能降级、工具错不崩题、答案可回溯 | 无 schema 层，工具是鸭子类型 |
| 2 | 证据闭环与安全收敛 | `diagnose/verifier.py`、`repair.py` | 失败五分法、修复有上限、空证据必拒答 | `diagnose/` 只有一个空 `__init__.py`；预算只有 `max_rounds` |
| 3 | 评测升级 | `eval/traces.py`、`replay.py` | 逐题可回放、同预算对照、按跳分层 | 落盘只有指标行，没有轨迹 |
| 7 | 可观测性（CompassRAG 侧） | trace schema、成本估算、三个故障案例 | 现象→根因→修复→回归→结果 | 有逐调用 token/延迟，无成本估算、无 run 关联键 |

## 1. 项目定位与面试标签

**一句话边界：** CompassRAG 管"问题内搜索"，Atlas 管"任务间状态"。CompassRAG 面向单轮/单问题检索，不承担跨任务长期状态。

**面试标签：** Agentic RAG、Tool Calling、Query Planning、Evidence Verification、成本控制。

**核心方法论（两项目共用）：** LLM 负责提出候选决策，确定性代码负责校验、执行、记录和恢复。

**目标态项目段**——以下是完成升级并拿到相应实验结果后的目标版本，括号中的数字必须替换成真实实验结果，**未完成前不能写入简历**：

> **CompassRAG——面向多跳问答的检索决策型 Agent｜独立开发**
> **技术栈：** Python、OpenAI SDK、Pydantic、LangGraph、BM25S、Sentence Transformers/BGE-M3、NumPy、PyArrow、结构化 Agent Tracing
>
> **项目描述：** 针对传统 RAG 固定"检索一次再生成"导致多跳证据缺失、无效检索浪费 token、证据不足却强行回答的问题，设计并实现一个以检索决策为核心控制流的 Agent。系统根据问题难度选择直答、单跳或多跳路径，动态生成检索计划，调用 BM25、稠密向量和实体关系图三类工具获取证据，并在每轮后判断证据是否足够，支持早停、追加查询和失败修复。
>
> **工具调用与检索规划：** 使用结构化输出约束路由器、查询分解器和证据判定器，统一封装检索工具的输入、输出、超时和错误类型；通过显式状态机管理 `plan → retrieve → verify → answer`，禁止模型绕过工具直接伪造证据，解析失败自动降级为安全的单轮检索。
>
> **多路检索与结构化索引：** 融合 BM25、BGE-M3 稠密检索和实体关系图 PPR 检索信号，使用 RRF 和近重去重形成候选证据集；实体图跨文档连接共享实体，补足纯相似度检索难以覆盖的第二跳证据。查询改写支持多跳子问题分解和 HyDE，所有查询变体保留来源和检索轮次。
>
> **证据闭环与成本控制：** 每轮根据原问题、已获得证据和缺失信息生成下一步查询；当证据满足回答条件时提前停止，达到轮数、token、延迟或费用上限时强制收敛。记录每次工具调用、检索轮次、输入输出 token、延迟、证据覆盖和停止原因，支持按题回放与失败归因。
>
> **评测结果：** 在 MuSiQue、2WikiMultiHopQA 和 HotpotQA 的固定子集上进行同题集消融，比较固定单轮 RAG、查询改写、实体图索引和自适应决策的增量；进一步统计答案 EM/F1、支撑段落覆盖率、全链路证据命中率、无答案拒答率、平均检索轮数、token 和单位问题成本。最终报告 `[EM/F1 提升]`、`[证据 full-hit 提升]` 和 `[成本/延迟变化]`，并提供逐题失败轨迹。

**建议口述主线：**

> CompassRAG 解决的是"Agent 应不应该继续查"的问题。普通 RAG 把检索当作固定前置步骤，而我的系统把检索当作 Agent 工具，让模型先判断问题难度，再决定查询计划和搜索深度。每一轮之后系统都检查证据是否真的支持原问题，不满足就生成下一条查询，满足就提前结束。模型只负责提出路由和查询，工具层负责召回，确定性评测负责验证证据覆盖，所以可以同时讨论效果、成本和失败原因。

## 2. 技术栈取舍

### 2.1 与 Atlas 共用的四条（只列与 CompassRAG 有关的）

- **Python 3.12：** 便于共享 Agent、检索、评测代码，面试时可以深挖实现细节。现状已满足（本机 3.12.13，`requires-python >=3.10`）。
- **OpenAI-compatible SDK：** 模型端点可替换；模型调用统一进入 telemetry，不绑定某一家服务商。现状已满足（`LLMClient` 读 `.env` 的 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`）。
- **Pydantic v2：** 约束路由、工具参数、状态操作、评测结果和错误对象。现状**未引入**——全部是 dataclass + 手写 `json.loads` + 正则截取。
- **结构化 tracing：** 记录 Agent 步骤、工具调用、token、延迟、停止原因和状态变化，而不是散乱文本日志。现状部分满足：`runs/telemetry.jsonl` 有逐调用的 model/token/延迟/finish_reason，但它是**调用级**的，不是**步骤级**的。

### 2.2 CompassRAG 专属

- **LangGraph：** 只承担 `plan/retrieve/verify/answer` 控制流和循环边；工具输出、证据 provenance 和评测数据由项目自己的 schema 管理。
- **BM25S + Sentence Transformers/BGE-M3：** 分别承担词法和稠密检索；不引入 LlamaIndex/Haystack 作为主抽象，以保持消融和遥测可控。
- **实体关系图 + PPR：** 作为检索信号，不把项目包装成"数据库项目"。
- **PyArrow/Parquet：** 用于语料和离线评测产物，避免把 JSONL 当成核心技术。

### 2.3 与现状的差距（实测）

`pyproject.toml` 现有依赖：`openai`、`python-dotenv`、`pyyaml`、`huggingface_hub`、`numpy`、`pyarrow`、`bm25s`；可选 `dense=sentence-transformers`、`dev=pytest`。

| 计划里的技术 | 现状 |
|---|---|
| pydantic | 未引入。所有结构化输出靠手写解析 + 降级 |
| langgraph | 未引入。控制流是 `agent.py` 两个方法 + 一个 `for` 循环 |
| BM25S / BGE-M3 / PyArrow / 实体图 PPR | **已在用**，这四项不是新增，是"包装成工具协议" |
| 结构化 tracing | 调用级已有，步骤级没有 |

结论：技术栈里真正的新增只有 **Pydantic** 和 **LangGraph** 两项，其余是给已跑通的东西补接口。

## 3. 现状基线（实测，2026-10-28）

### 3.1 代码与测试

- `compassrag/agent.py` 160 行；scripted 与 decision 两种形态分在 `answer()` 和 `_agentic_answer()` 两个方法里，证据累积/截断/答案调用/token 聚合各写一遍。
- 116 个测试全绿，1.25 s（`.venv/bin/python -m pytest -q`）。其中 `tests/test_agent.py` 14 个、`tests/test_decision.py` 5 个——**这两组是阶段 1 重构的回归锚**。
- `compassrag/diagnose/` 存在，只有一个空 `__init__.py`。
- 不存在 `compassrag/agent/` 目录；`compassrag/agent.py` 是**模块**（这一点在 §9.1 会变成结构问题）。

### 3.2 现有消融表（MuSiQue 50 题 / samples 小库 4646 块 / 同题集）

| 配置 | EM | F1 | 证据召回@10 | token/题 | 备注 |
|---|---|---|---|---|---|
| naive | 0.300 | 0.422 | 0.720 | 3927 | scripted 单轮 + BM25/dense 两路 RRF |
| +rewrite（②） | 0.400 | 0.553 | 0.720 | 4541 | 分解 + HyDE 变体 |
| +kg（③） | 0.400 | 0.544 | **0.813** | 3346 | 唯一提升证据覆盖的行 |
| +decision（①） | **0.480** | **0.662** | 0.537 | **16071** | 路由 + 多跳 + 早停 |
| +full（①+②+③） | 0.500 | 0.621 | 0.538 | 14682 | 收官行 |

数据来自 `runs/qa/musique__*__samples.jsonl`；同名 `_invalid_*` 两行是已作废口径，不参与。

### 3.3 三条必须先承认的事实

这三条决定了后面各阶段的优先级，也是面试时"哪些结果是负面的或口径受限的"的答案来源。

1. **决策层的 +18pp EM 是 4.1× token 换来的，而且证据召回反而降了。** 16071 vs 3927 token/题；证据召回 0.720 → 0.537。在没有同预算对照之前，只能说"多查几轮在小库上更准"，**不能说"检索决策更好"**。这正是阶段 3 第 3 项存在的理由。
2. **只有 MuSiQue 有 QA 结果。** `data/samples/` 三基准各有 300 题抽样（seed42），但 `runs/qa/` 只有 musique；2wiki/hotpotqa 只建了 full_dev 语料（58510 / 66946 块，musique 21118），**没有 samples 小库、没有查询 NER 缓存、没有实体图**（`data/cache/kg/` 只有 musique）。三基准并列的瓶颈在实体图抽取，不在跑评测。
3. **现在落盘的是指标行，不是轨迹。** 每行只有 `id/stratum/pred/gold/em/f1/ev_recall/rounds/prompt_tokens/completion_tokens/n_calls/finish_reasons/answer_empty/n_queries/route/n_llm_calls`——**没有 queries 列表、没有 evidence chunk id、没有配置快照、没有模型名、没有 seed**。阶段 3 的 replay 不是"补个脚本"，是"重新定义落盘格式"，而且旧数据补不回来。

> 重跑成本（用来判断"能不能事后补"）：五配置各跑 50 题，合 42567 token/题 → 一轮 **≈213 万 token**；做三基准就是三倍，加重复运行再翻。所以轨迹格式必须一次定对，否则每次补字段都要重跑。

## 4. 阶段 0：CompassRAG 侧边界与统一接口

**目标：** 先把 CompassRAG 的职责、数据流和接口定下来。

**产出：**

- CompassRAG 架构图：Agent 控制流 → 工具注册 → 多路检索 → 证据验证 → 评测。
- 统一运行事件字段（两项目共用）：`run_id`、`step_id`、`parent_step_id`、`agent_role`、`tool_name`、`input_hash`、`latency_ms`、`token_usage`、`outcome`。
- 〔修正〕`docs/agent-projects-boundary.md` 降为可选项：CompassRAG 侧先在自己的架构图/README 里写清边界；双项目对比文档等 Atlas 进到阶段 5 再补，不要在只有一方成型时写两份的对比。

**验收：** 能用一句话解释区别：CompassRAG 管"问题内搜索"，Atlas 管"任务间状态"。

> 现状（实测）：README 已有架构图与五行消融表（commit 9a8608a）；`AgentResult` 已聚合 prompt/completion token、轮数、查询、路由；`runs/telemetry.jsonl` 逐调用记录 model/token/延迟。缺的正是 `run_id`/`step_id`/`parent_step_id`/`input_hash`/`outcome` 五个字段——而它们就是阶段 1 要设计的 `AnswerTrace` 主体。**所以阶段 0 的接口部分并进阶段 1 一起做，不单独占一个里程碑。**

## 5. 阶段 1：工具调用化

> **进度（2026-10-28）**：6/7 项完成，1 项暂缓（LangGraph）。实现落在 `compassrag/agent/{schemas,tools,state,core}.py` + `eval/traces.py`；测试 137 passed（原 116 + 新增 21）。

**目标：** 把当前 Agent 主循环升级为一个可解释的工具调用 Agent。

**建议文件**〔修正〕：

- Create: `compassrag/agent/__init__.py`（把现有 `agent.py` 整个迁进来，并保留 `from compassrag.agent import Agent, AgentConfig, AgentResult` 这条导入路径）
- Create: `compassrag/agent/state.py`、`compassrag/agent/tools.py`、`compassrag/agent/schemas.py`
- Delete（迁移后）: `compassrag/agent.py`
- Modify: `compassrag/decision/planner.py`、`compassrag/decision/sufficiency.py`
- Create: `tests/test_agent_tools.py`
- Modify: `tests/test_agent.py`（仅补新路径用例，旧用例一行不改）

〔修正理由〕实测（Python 3.12.13）：同一目录下 `pkg/__init__.py` 与 `pkg.py` 并存时，`import pkg` 拿到**包**，`pkg.py` 变成不可达的死代码，且不报错。原文档"Create `agent/state.py` + Modify `agent.py`"会让 160 行已测逻辑静默失效——`tests/test_agent.py` 那 14 个用例要么全红（包没 re-export）要么测的是另一个实现。所以只有两条路：整体迁包（推荐，一次性），或新模块放包外（`compassrag/agent_tools.py` 等平级文件，改动最小但 `agent.py` 会涨到 400 行以上）。

**实施项**（原文逐条，附现状）：

- [x] 定义 `RouteDecision`、`SearchPlan`、`SearchRequest`、`EvidenceCheck`、`StopReason` 和 `AnswerTrace`。
  ✅ 2026-10-28：`compassrag/agent/schemas.py`（Pydantic v2；`Route`/`QuerySource`/`StopReason` 用枚举，停止原因含四类 `budget_exhausted:*`）。
  现状：`Plan`（route + subquestions + calls）与 `Sufficiency`（ok + next_query + calls）两个 dataclass 已存在；`StopReason`、`AnswerTrace` 完全没有；`route` 是裸字符串常量（`ROUTE_DIRECT/SINGLE/MULTI`）。
- [x] 将 BM25、dense、KG 检索封装成统一工具协议：输入 query、top_k、filters；输出 chunk id、score、source、retriever、rank。
  ✅ `compassrag/agent/tools.py`：`ToolSpec.run(SearchRequest) -> ToolCall`，保留 retriever/rank；旧的 `getattr(tools["dense"], "vector")` 反查改成 `ToolSpec.vector()` 代理。
  现状：四个检索器签名已一致（`search(query, k) -> list[(chunk_id, score)]`，见 `retrieval/bm25.py:28`、`dense.py:131`、`index/kg.py:355`、`index/wiki.py:253`），但输出丢了 `retriever` 和 `rank`，且三路 score 语义完全不同（BM25 原始分 / 余弦相似度 / PPR 分数）——现在靠 RRF 只用排名才没暴露问题。另有一个现成证据说明协议不正式：`agent.py:127` 为了做近重合并，要 `getattr(self.tools.get("dense"), "vector", None)` 反查工具的内部方法。
- [x] 为每次检索保存 `query_source`，区分原问题、子问题、sufficiency 生成查询和 HyDE 变体。
  ✅ `compassrag/query/rewrite.py::build_query_variants_sourced`（original/subquestion/hyde）+ 决策路径的 `sufficiency` 来源；每条查询还记 `new_evidence`。
  现状：`build_query_variants` 返回扁平 `list[str]`（`query/rewrite.py:82`），来源在函数内部就丢了；`AgentResult.queries` 也只记字符串。
- [ ] 让 planner 和 sufficiency checker 只输出 schema，不允许返回自由文本控制命令。
  ◐ 部分：控制流边界已收进 schema（`_plan` 立刻构造 `RouteDecision`、判定构造 `EvidenceCheck`）；解析器内部仍是手写 json+正则 + 安全降级（**故意保留**，见 `decisions/D2`）。
  现状：事实上已是 JSON-only，但解析是手写正则 + `json.loads`（`planner.py:52`、`sufficiency.py:37`），不是 schema 校验；两处失败都降级为安全值（single / `sufficient=true`）。**改 Pydantic 时必须原样保留两个机制**：①解析失败→安全降级；②预算阶梯重试 1024→2048→4096 + 收尾提示（`PLAN_BUDGETS`/`SUFFICIENCY_BUDGETS`）。这两条是实测救回"思维链吃光预算出空答案"的手段，不能顺手丢掉。
- [x] 为每个工具增加超时、空结果、索引缺失和参数非法的显式错误。
  ✅ outcome ∈ {ok, empty, error, timeout, invalid_params, index_missing}，异常不再抛穿 `Agent.answer`；超时默认关闭、开启后线程池强制返回。
  现状：没有。检索工具是本地 numpy/bm25s 调用，无超时概念；LLM 侧超时与重试在 `LLMClient`（SDK 级 `timeout=120`、`max_retries=4`），异常直接抛穿 `agent.answer`——**一题出错整轮评测中断**，这正是本条验收"工具超时不导致整题崩溃"要对的东西。
- [x] 将 `scripted` 与 `decision` 统一到同一状态图，通过开关关闭决策节点，而不是维护两套逻辑。
  ✅ 单条 `plan → (retrieve → verify)* → answer → finish`；`use_decision` 只决定计划/验证节点是否启用。旧模块 `agent.py` 已迁成包 `compassrag/agent/`（`lessons/L2` 的遮蔽坑已避开）。
  ✅ 等价校验：在真实 BM25 索引上对比第 1 代实现，naive / rewrite / decision 三配置的证据、轮数、查询、LLM 调用数、token **逐位一致**。
  现状：`answer()` 与 `_agentic_answer()` 两个方法各写一遍累积、截断、答案调用与 token 聚合。
  ⚠ **守卫条件（补充）：** 统一后 naive 行必须仍能复现 EM 0.300 / F1 0.422 / 3927 token/题。五行消融表是简历数字的一半来源，历史行一旦不可比，整张表要重跑（≈213 万 token/轮）。落地方式：先让 `tests/test_agent.py` 现有 14 个用例做重构等价锚，再动手。
- [ ] 使用 LangGraph 表达 `plan/retrieve/verify/answer` 控制流；状态中保存经过 schema 校验的 `AgentState`，证据和 provenance 仍由项目自己的数据结构管理。
  ⏸ 暂缓：本轮不引入依赖（见 `decisions/D3`）；`AgentState` 已与框架解耦，接入时只换跳转不改状态。
  现状：无依赖，控制流是 `_agentic_answer` 里一个 `for` 循环 + `pending` 队列（约 30 行有效逻辑）。是否引入见 §9.2。

**验收：** 模型输出格式错误时能降级到单轮检索；工具超时不导致整题崩溃；每个答案可以列出完整的计划、查询、工具、证据和停止原因。

> 现状（实测）：第一条**已经成立**（planner 失败降级 single、sufficiency 失败降级"充分"）；第二条、第三条不成立——没有工具错误分类，也没有 `AnswerTrace` 可列。

## 6. 阶段 2：证据闭环与安全收敛

> **进度（2026-10-28）**：6/6 项完成。实现落在 `compassrag/diagnose/{verifier,repair}.py` + `agent/core.py`；测试 155 passed（新增 18）。默认 `use_verifier=False`，历史消融行不受影响。

**目标：** 让 Agent 不只是"搜到了就回答"，而是能判断证据是否支持结论。

**建议文件：**

- Create: `compassrag/diagnose/verifier.py`
- Create: `compassrag/diagnose/repair.py`
- Create: `tests/test_diagnosis.py`
- Modify: `compassrag/agent.py`（迁包后为 `compassrag/agent/__init__.py`）

**实施项**（原文逐条，附现状与建议）：

- [x] 让答案生成器同时返回结构化 claim 列表和证据引用，而非只有答案字符串。
  ✅ 以**独立 verifier 调用**实现（`diagnose/verifier.py`），答案协议不变：`Claim` 由模型提出，**引用编号是否真实存在由代码判定**（越界/引空/不存在一律记不支持），见 `decisions/D4`。
  现状：答案是一次 chat 调用，提示词要求"a short, direct answer (a few words)"，证据以 `[1] title: text` 平铺（`agent.py:19-26`、`154-160`）。
  〔建议〕**不要改答案生成器的输出协议**，改成"先出答案（不变），再由独立的 verifier 调一次，把答案拆成 claim 并逐条找支持 chunk"。理由：现在 EM/F1 是 SQuAD 短答口径，一旦让同一次调用附带 claims 和引用，prompt 变了、输出变长了，五行消融表的可比性立刻失效（这跟阶段 1 的守卫条件是同一件事）。代价是每题多一次调用，需在成本列里体现。
- [x] 对每条 claim 检查是否存在支持它的 chunk；支持不足时输出 `insufficient_evidence`。
  ✅ `VerificationResult.needs_repair` 触发修复；修一次仍不成立 → 答案改写为 `unknown` + `StopReason.INSUFFICIENT_EVIDENCE`。
  现状：无。现有只有"答案是否为空"和"是否给出 unknown"两个弱信号。
- [x] 将失败分成 query miss、evidence gap、source conflict、answer unsupported、tool failure 五类。
  ✅ `diagnose/repair.py::classify_failures`：前三类规则判定（全工具挂 / 全部查询零新证据 / 判定说不够却无下一条查询），后两类来自 verifier。
  〔建议〕五类里只有 `answer unsupported`、`source conflict` 真正需要模型判断，其余三类**应做成规则判定**：query miss = 本轮检索相对已有证据无新增 chunk；evidence gap = 支撑标题未覆盖（`full_hit_at_k` 已有实现）；tool failure = 工具层显式错误。规则判定的好处是便宜、可复现、可回放——也才符合"确定性代码负责校验和记录"的方法论。
- [x] 为每类失败定义有限修复动作：改写查询、补检索、切换检索视图、拆分子问题或拒答。
  ✅ `REPAIR_FOR` 一一映射 + `choose_repair_query`（未支撑断言原文优先，确定性、不额外调模型）；`max_repairs` 用尽一律 `REFUSE`。
  现状：只有一个隐式修复动作——sufficiency 返回 `next_query` 就再查一轮，上限 `max_rounds`。
- [x] 设置全局上限：最大轮数、最大 token、最大延迟、最大估算费用；触顶后输出可解释的 `budget_exhausted`。
  ✅ `agent/state.py::Budget` 四类上限 + 四类 `budget_exhausted:*` 停止原因；token 预算把 plan/verify/answer/retry 全部计入。
  现状：**只有 `max_rounds`**（另有 `top_k`、`max_evidence` 两个容量参数）。`AgentConfig` 里没有任何 token/延迟/费用预算。注意预算要把"空答案重试阶梯"算进去（`answer_retry_budgets=(2048, 4096)`），它本身是成本大头之一。
- [x] 对 null query 或证据为空的题，要求 Agent 选择拒答/unknown，不允许强行生成事实。
  ✅ `refuse_on_empty_evidence`：无证据时**一次模型调用都不发**，直接 `unknown` + `empty_evidence`（默认关闭以保持历史行可比，`plus_verify.yaml` 可开）。
  现状：提示词里已有"如果证据不足回答 unknown"，但**不是强制**，也没有单独统计拒答准确率——`runs` 里只有 `answer_empty`，不是拒答。

**验收：** 失败轨迹能解释"为什么继续查、为什么停止、为什么拒答"；修复循环有明确上限；空证据和矛盾证据不会被误标为成功回答。

## 7. 阶段 3：评测升级

> **进度（2026-10-28，未花一分钱）**：脚手架齐了——`eval/manifest.py`（配置/模型/语料指纹/种子/git）、`eval/report.py`（四列表 + 分层 + 可比性警告）、`scripts/report_runs.py`、`scripts/check_benchmarks.py`、`eval_qa.py --max-total-tokens`。**待付费运行**：三基准跑数、同预算对照、重复运行。

**目标：** 把已有消融表升级为 Agent 开发岗位最看重的过程评测。

〔修正〕**顺序前置：** 本阶段的 `traces.py`（trace 落盘格式）应与阶段 1 的 `AnswerTrace` 一起设计、一起落地，不要等阶段 1、2 做完再来补。理由见 §3.3 第 3 条与 §10。

**建议文件：**

- Create: `compassrag/eval/traces.py`
- Create: `compassrag/eval/replay.py`
- Create: `tests/test_trace_replay.py`
- Modify: `compassrag/eval/metrics.py`
- Modify: `scripts/eval_qa.py`

**实施项**（原文逐条，附现状）：

- [x] 保留已有同题集消融：naive、rewrite、KG、decision、full。
  ◐ 配置齐全，但只有 MuSiQue 出数（50 题）；2wiki/hotpotqa 缺 samples 语料，见 `scripts/check_benchmarks.py`。
  现状：`configs/ablation/` 五套配置齐全（另有 `plus_wiki.yaml` 作为负结果复现）。但**只有 musique 出数**，且只跑了 50 题。
- [x] 增加过程指标：计划正确率、工具调用成功率、证据 full-hit、claim support rate、无答案拒答准确率、平均轮数、提前停止率、失败修复成功率。
  ✅ 除**计划正确率**（需要人工标注的计划金标，本语料没有）外全部落进派生行：`ev_full_hit` / `claim_support_rate` / `refused_rate` / `rounds_per_q` / `early_stop_rate` / `repair_success_rate` / `tool_failure_rate`。
  现状：EM/F1/`recall_at_k` 在用。八项指标的真实状态：四项**完全不存在**（计划正确率、工具调用成功率、claim support rate、失败修复成功率）；两项**有原料但没进表**（平均轮数＝`rounds` 字段已有、聚合没算；证据 full-hit＝`full_hit_at_k` 已实现但 `scripts/eval_qa.py` 没调用，只 import 了 `recall_at_k`）；两项**要新定口径**（无答案拒答准确率——现在只有 `answer_empty`，不是拒答；提前停止率——只能事后从 `rounds < max_rounds` 推，没有字段）。
- [x] 做同预算对照：固定 token/延迟预算，比较"多查"是否真的带来更高答案质量。
  ◐ 脚手架就位（`AgentConfig.max_total_tokens` / `eval_qa.py --max-total-tokens`），**跑数要付费**，等确认。
  现状：无法做——没有 token/延迟预算开关。这是本阶段唯一需要动 agent 主循环的项，也是最能回答 §3.3 第 1 条那个"4.1× token"质疑的项。
- [x] 做按跳数分层统计：1-hop、2-hop、3-hop、4-hop；不要只报总平均。
  ✅ `eval/report.py::stratified` + `report_runs.py --stratify`；**口径按基准分支**（`lessons/E1`）：只有 musique 的 stratum 是支撑段落数。
  现状：`stratum` 字段已有，但语义**随基准变**（`corpus/benchmarks.py:8`）：musique=支撑段落数、hotpotqa=level、2wiki=题型。所以"按 1/2/3/4-hop 分层"目前只有 musique 能直接做（且是支撑段落数近似）；另两个基准要另写分层口径，不能混用同一张图。
- [x] 每个 run 写完整配置快照、模型信息、语料版本和随机种子。
  ✅ `eval/manifest.py`：配置快照 + 模型/嵌入模型 + 语料块数与 id 指纹 + 抽样指纹 + 种子 + git commit/是否脏；`fingerprint()` 不同的 run 不可直接对比。
  现状：四项全无。`runs/telemetry.jsonl` 里有 model，但与 `runs/qa/*.jsonl` 的行**没有关联键**（没有 run_id）。
- [x] 保存逐题 trace，支持 replay，不重新请求模型也能重算证据和停止指标。
  ✅ 2026-10-28：`compassrag/eval/traces.py`（两层落盘：trace + 派生行；`derive_row` / `replay_rows` / `aggregate`），`scripts/eval_qa.py` 已改为写轨迹 + 派生指标行。
  现状：不满足，见 §3.3 第 3 条。要改成两层落盘：`trace`（每步 LLM 调用/工具调用/证据/停止原因，可回放）+ `row`（由 trace 派生的指标行，可随时重算）。
- [ ] 把当前 50 题结果作为历史小样本，不再扩写成稳定结论；正式结果至少使用固定三基准、重复运行和明确 n 值。
  ◐ 报告层已会对"同基准样本量不一致"报警；三基准跑数待批准（`check_benchmarks.py` 列出缺口）。
  现状：50 题、单次运行、单基准。三基准的**前置成本在语料与实体图**（2wiki/hotpotqa 无 samples 小库、无查询 NER 缓存、无 KG），不在评测脚本。见 §9.4 的取舍建议。

**验收：** 一条失败记录可在本地重放；一张结果表同时包含质量、证据、成本和可靠性；所有对照使用相同题集和预算。

## 8. 阶段 7：可观测性与面试证据（CompassRAG 侧）

> **进度（2026-10-28）**：三个故障案例已写成口述材料 `docs/failure-cases.md`（现象→根因→修复→回归→结果 + 90 秒口述）；成本估算器与聚合报告已就位；**简历数字口径说明**要等三基准跑完才能定稿。

**目标：** 能回答"这一步为什么发生、花了多少、失败在哪里"。

**实施项（CompassRAG 侧）：**

- [ ] 统一 JSON trace schema，CompassRAG 保存自己的业务字段（route、query_source、retriever、rank、stop_reason、failure_class）。
- [ ] 记录模型名称、prompt/completion/reasoning token、延迟、重试、工具调用、错误和停止原因。
  现状：`Telemetry` 已逐调用记录 model、prompt/completion/total token、`reasoning_tokens`、`latency_ms`、`finish_reason`、`error`；**工具调用完全没有记录**；重试只能从 tag 间接看出（`plan_retry`/`sufficiency_retry`/`answer_retry`），没有次数、也没有"为什么这么取舍"的记录；`finish_reason` 是模型级的，不等于 Agent 级的 stop_reason。
- [ ] 增加成本估算器，按模型配置计算单题成本（token → 费用）。
  现状：无。现在只能报 token。
- [ ] 生成两类报告：逐题 trace 和聚合指标表。
- [ ] 准备至少三个故障案例，形成"现象 → 根因 → 修复 → 回归测试 → 结果"的口述材料。
  〔补充〕现成候选有三个，都有 commit 与运行产物可引：① 思维链吃光 512 预算导致约 27% 空答案（`runs/diag/`）；② 实体图抽取事故（全量 3584 调用/167 万 token，已叫停并立预估规矩）；③ 三处变体/NER/过滤 bug 导致的召回虚高（commit f0867a5）。这三个比编造的案例更好用，因为每个都能落到具体行号与日志。
- [ ] 所有简历数字注明样本量、对照组、预算和运行次数。

## 9. 需要先拍板的四个问题（补充）

这四条不是原计划的失败项，而是原计划没有展开、但会决定改造量和排期的分歧点。建议开工前逐条定，避免做到一半返工。

### 9.1 `agent.py` 怎么办：迁包 or 平行文件

- **A（推荐）迁包：** `compassrag/agent/{__init__,state,tools,schemas}.py`，`__init__` re-export `Agent/AgentConfig/AgentResult`，删掉 `agent.py`。干净，但所有 `from compassrag.agent import ...` 的调用点（`scripts/eval_qa.py:18`、`tests/test_agent.py`）要一起确认。
- **B 平行文件：** 新建 `compassrag/agent_state.py`、`agent_tools.py`、`agent_schemas.py`。改动最小，`agent.py` 继续存在但会涨到 400 行以上，模块职责变糊。
- 判据：如果阶段 1 还要改一次控制流（跑 LangGraph），选 A；如果只想先补 schema 层，选 B 也够。

### 9.2 LangGraph 要不要引入

现状控制流约 30 行（`_agentic_answer` 的 `for` + `pending` 队列），已经能表达"多轮检索 + 早停 + 追加查询"。LangGraph 带来的是可视化、检查点、可回放——但 CompassRAG 明确不承担跨任务恢复（那是 Atlas 的事），检查点对它价值有限。

- **引入的理由应当是：** 控制流能被面试追问着讲清楚（图 + 显式边 + 状态），且后续扩 `verify`/`repair` 节点时不用手写分支。
- **不引入的理由：** 方法论是"确定性代码负责执行与记录"，手写状态机 + 显式 `StopReason` 反而更贴合；少一个依赖，少一层与 LangGraph 状态模型的对接（工具输出与 provenance 还得自己管）。
- 这条决定阶段 1 的最后一条实施项保不保留，也决定技术栈里能不能写 LangGraph。

### 9.3 答案协议改不改（claim 一起出 vs 二次校验）

见 §6 第 1 项。〔建议〕二次校验。代价：每题 +1 次 LLM 调用；收益：五行消融表的 EM/F1 保持可比，历史行不用重跑。

### 9.4 三基准做到什么程度

三条路，成本差一个数量级：

- **全量对齐：** 为 2wiki/hotpotqa 建 samples 小库 + 向量 + BM25 + **实体图**。实体图是瓶颈（`data/cache/kg/` 整体已 837M = full_dev 670M + samples 166M，samples 那个 `node_vectors.npy` 单文件就 161 MiB），且 2wiki/hotpotqa 语料块数是 musique 的 2.8–3.2 倍。
- **降级对齐（推荐）：** 三基准只跑**不含 KG** 的行（naive / +rewrite / +decision / +decision+rewrite），KG 的增量只在 musique 上单独报，并在表注里写清"KG 行仅 MuSiQue"。这样"三基准"用来证明**决策层的泛化**，KG 的贡献用单基准消融证明——两件事本来就不必挤在同一张表里。
- **保持单基准：** 只在 musique 做，但把 n 提到 300、做多次运行。诚实但弱。

## 10. 建议实施顺序与里程碑（补充）

```
M0  接口与轨迹格式          ── 阶段0 接口部分 + 阶段1 的 AnswerTrace + 阶段3 的 traces.py 一并设计
      ↓
M1  工具调用化 + 单轮/多轮统一 ── 阶段1（含回归锚：naive 行必须仍是 0.300/0.422/3927）
      ↓
M2  证据闭环与预算            ── 阶段2（含 token/延迟/费用上限，同预算对照的前提）
      ↓
M3  评测升级                 ── 阶段3（三基准 + 重复运行 + 过程指标 + replay）
      ↓
M4  面试材料                ── 阶段7 的三个故障案例 + 数字口径核对
```

与原文档的差异就一条：**trace schema 从 M3 提到 M0**。理由是阶段 1 的 `AnswerTrace`、阶段 2 的失败分类、阶段 3 的 replay 用的是同一份数据；如果 M1/M2 用临时格式记录，M3 就得把 M1/M2 的实验全部重跑才能拿到轨迹（≈213 万 token/轮 × 基准数 × 重复次数）。定义格式比补脚本便宜得多。

每个里程碑的"可验证产出"：

| 里程碑 | 能拿出手的东西 |
|---|---|
| M0 | 一份 `AnswerTrace` JSON schema + 一道题的真实 trace 样例 |
| M1 | 格式错/工具错两个故障注入用例通过；`pytest` 全绿且 naive 行数字不变 |
| M2 | 一张"失败类别 × 修复动作 × 触发上限"的表 + 两道题的完整失败轨迹 |
| M3 | 一张同时含质量/证据/成本/可靠性四列的表 + 一条可在本地重放的失败记录 |
| M4 | 三个故障案例各 90 秒口述 + 数字口径说明（样本量/对照/预算/次数） |

## 11. 面试必答清单与当前可答性

### 11.1 CompassRAG 必答问题（原文）

| 问题 | 现在能答吗 | 缺什么 |
|---|---|---|
| 为什么不能固定检索一轮？什么时候 direct route 会误判？ | 能（有 plus_decision 行与路由分布） | direct 误判率没有单独统计 |
| BM25、dense、KG 三路信号分别解决什么问题？为什么用 RRF？ | 能（三路消融齐全） | — |
| HyDE 可能引入幻觉，为什么仍然使用？如何限制它的影响？ | 部分 | 变体来源没有落盘，说不清"哪条变体起了作用" |
| 证据充分度判定器如何避免"模型说充分但其实没有支持"？ | **不能** | claim-level 校验（阶段 2 第 1–2 项） |
| 如何保证模型输出的 query 不会导致无限检索或费用失控？ | 部分（只有 `max_rounds`） | token/延迟/费用上限（阶段 2 第 5 项） |
| full-hit、claim support rate、答案 F1 之间是什么关系？ | **不能** | full_hit 没进评测，claim support 不存在 |
| 当前实验中哪些结果是正面的，哪些是负面的或口径受限的？ | 能（§3.3 三条） | 需要主动讲，不能等追问 |

### 11.2 共同追问（CompassRAG 侧）

- 什么情况下应该用 LangGraph，什么情况下自己写状态机？（§9.2 就是这道题的答案）
- 如何区分模型问题、检索问题、状态管理问题和评测问题？（靠 §6 的失败五分类）
- 如果 token 预算减半，系统哪些行为会改变？（做完同预算对照才能答）
- 如果模型换成另一个 OpenAI-compatible endpoint，哪些模块不需要修改？（`LLMClient` 已隔离；planner/sufficiency/answer 的提示词与解析是模型相关的，`finish_reason=length` 的预算阶梯尤其相关）

## 12. 交付门槛

- 工具调用有 schema，检索循环有上限，答案有证据引用，失败可分类，trace 可回放，消融和过程指标可复现。
- 原有测试全部通过（当前基线：116 passed / 1.25 s）；新增测试覆盖 schema、工具失败、预算触顶和 replay。
- 简历所有数字都有实验记录，并注明样本量、对照组、预算和运行次数。
- 能讲 90 秒主线，并在 10 分钟内回答架构、失败、成本、评测和取舍问题。

## 13. 明确不在本计划内

- Atlas 的全部内容：状态图持久化（阶段 4）、显式编排与故障恢复（阶段 5）、上下文与状态治理（阶段 6）。
- 双项目的对比文档 `docs/agent-projects-boundary.md`（可选项，等 Atlas 成型再写）。
- 前端/页面。本项目的可解释性由 trace 与报告承载，不做 UI。
- 把 JSONL、Streamlit、pytest 写进技术栈第一行——它们分别是导出格式、调试工具、测试框架，属于说明而不是技术亮点。
