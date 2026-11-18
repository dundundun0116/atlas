# Atlas 项目面试准备

候选人介绍与高频技术追问

版本日期 2026 年 11 月 19 日

## 怎么使用这份材料

面试官通常先让候选人介绍项目，再围绕简历里的技术词追问原理、项目实现和取舍。下面按这个思路准备 18 个主题，不追求题海。每个回答先说概念，再落到 Atlas 的实现，最后说明边界。建议先熟悉项目介绍和 RAG 主链路，再挑与岗位相关的主题深入。

Atlas 可以概括为一个面向多步骤研究问答的 RAG Agent：CompassRAG 负责从资料中找证据，Atlas 负责组织研究步骤、传递结果和保存执行状态。当前主流程是固定研究 DAG，检索内部提供关键词检索、可选向量检索、查询增强与证据核验能力。它是个人项目与研究原型，不应包装成已验证的大规模生产平台。

## 项目介绍

### 一分钟介绍

我做的项目叫 Atlas，是一个面向多步骤研究问答的 RAG Agent。它把检索层 CompassRAG 和任务运行时 Atlas 组合起来：CompassRAG 从本地资料中召回带来源的证据，Atlas 再按 retrieve、draft、verify、必要时 repair、final verify 的步骤完成研究，并持久化步骤和工具调用结果。

检索上我实现了 BM25 和可选的 BGE M3 稠密检索，用 RRF 融合排名；对于复杂问题，可以通过查询改写和多跳检索逐步补证据。答案生成与核验分开，引用结构由代码检查，语义支持由模型判断。运行时把步骤、尝试和工具调用保存到 SQLite，遇到未知调用结果时先对账，避免把超时直接当成失败重发。

我把它定位为研究原型：重点是把证据、执行步骤和恢复状态做成可检查对象。当前 DAG 是固定模板，稠密检索是 NumPy 精确扫描，核验也不是所有配置默认强制开启；项目仍需要更完整的端到端质量评估。

### 三分钟介绍

这个项目想解决的是：一个研究问题往往需要查不止一段资料，系统不仅要回答，还要说明依据，并且在检索、生成、核验等多步执行中保存进度。

系统分成两层。CompassRAG 是检索工具，负责资料切分与索引、查询增强、候选召回、排名融合和可选核验，返回答案、证据片段、来源和 trace。Atlas 是研究运行时，通过 ResearchTool 协议调用检索层，再用显式 DAG 管理 retrieve、draft、verify、repair 和 final_verify。当前顶层 DAG 由固定模板生成，不是模型任意规划工作流。

检索的基础路径是 BM25 加可选 dense。BM25 对专名和精确词项有效，dense 用归一化 BGE M3 向量表示语义相近内容。两路结果通过 RRF 按排名融合，因为 BM25 分数与向量相似度不在同一尺度。遇到多跳问题时，可以生成子问题并逐轮补充证据；HyDE 产生的假设文本只作为查询，不作为事实来源。

生成后，系统可以调用 verifier 做 claim 级检查：模型判断结论是否被证据支持，代码检查引用编号是否合法。两者职责不同，合法引用并不自动证明语义正确。Atlas 的审核步骤则检查研究草稿并给出问题，必要时有限修订一次。

执行状态保存在 SQLite，包括计划步骤、每次尝试和工具调用。远程调用超时可能意味着“结果未知”，所以不能一律重发；如果工具支持查询，就对账并复用结果，否则保持未知并等待人工判断。测试证明的是实现机制和已覆盖的故障场景，不等于证明所有研究问答都比普通 RAG 更准确。

## 高频技术问题

### 1. 你项目里的 RAG 整体流程是什么

我按离线和在线两条链路解释。

离线阶段先读取资料，按段落和句子近似切成 chunk，保留标题、来源和 chunk_id。BM25 为文本建立关键词索引；如果启用 dense，就用 BGE M3 编码并保存归一化向量。索引构建和资料上传是分开的能力，当前上传入口支持 txt、md、json，服务加载路径主要接了 BM25。

在线阶段接收问题，可以保留原问题并生成改写、子问题或 HyDE 查询；检索器各自召回候选，再通过 RRF 融合排名、按 chunk_id 去重。多跳模式会根据已有证据继续检索。最后把真实资料中的证据交给模型生成答案，可选运行 claim verifier，并返回来源与检索 trace。

接到 Atlas 后，检索成为研究 DAG 的 retrieve 步骤，证据以 refs 保存在 Attempt 和 ToolCall 结果里，再通过 data binding 传给 draft、verify 和 final_verify。当前这些 refs 不会由 DAG 自动全部写成状态图里的 Fact。

### 2. Prompt 是怎么设计的，Prompt 由哪些部分组成

我把 Prompt 看成一个输入契约，通常包含角色和任务、必要上下文、用户问题、可用证据、约束以及输出格式。不是把所有内容都塞进一段 instruction，而是按调用目标分别组织。

Atlas/CompassRAG 里至少有几类 Prompt：答案生成 Prompt 告诉模型回答问题并遵循短答案或研究草稿格式；多跳决策 Prompt 让模型选择 single、multi 或 direct、产生子问题并判断是否继续；充分度 Prompt 根据已有证据判断还缺什么；verifier Prompt 把答案拆成 claims，要求给出证据编号和支持判断；Atlas Auditor Prompt 则审核草稿并返回 verdict 和 issues。

证据会作为带编号的资料片段传入，要求模型把它当作依据而非指令。HyDE 文本仅用于检索，不能混入最终证据。结构化输出会做 JSON 解析和字段检查。不过不同调用的 Prompt 和约束并不完全相同，claim verifier 默认关闭，Atlas 的 ContextPackage 也不是整段最终 Prompt 的完整存档，所以我不会声称所有模型调用共用同一套严格模板。

### 3. 为什么要用 RAG，而不是直接把资料放进 Prompt 或微调

资料少、固定且能装进上下文时，直接提供全文是很好的基线，链路也更简单。资料规模变大或需要定位来源时，RAG 可以按问题取相关片段，降低无关上下文和输入成本，并让答案能指向来源。

微调更适合改变模型稳定的行为或领域表达，不适合承载经常更新、需要追溯的知识。Atlas 的目标是从可变资料中取证并完成多步研究，因此先用 RAG。项目目前没有证明 RAG 在所有问题上都优于长上下文；是否选 RAG，要看语料规模、更新频率、引用要求和延迟成本。

### 4. Chunk 怎么切，为什么这个参数

当前基准语料的默认 chunk 上限是 1800 字符，overlap 预算是 200 字符，尽量沿段落和句子边界处理。Overlap 是为了避免一个事实刚好被边界切开；chunk 太小会丢上下文，太大则噪声和 Prompt 成本上升。

这是一套工程默认值，不是实验得出的普遍最优值。实现按字符近似切分，句界规则对英文标点更成熟，超长句也可能超过预期；dense encoder 默认 max_length 为 512，字符长度和模型 token 长度不是同一个概念。面向中文或长资料，需要比较不同切分策略下的证据召回、完整命中、答案质量、延迟和 token 成本。

### 5. BM25 和向量检索各解决什么问题，为什么混合

BM25 是词项匹配方法，利用词频、逆文档频率和文档长度归一给文档打分，适合专名、编号和精确术语。向量检索把查询和 chunk 编码到同一向量空间，适合词面不同但语义相近的表达。

Atlas 使用 bm25s 做 BM25，dense 路径用 BGE M3 的归一化单向量表示。两者可能互补，所以可以混合；但混合不是必然提升，dense 可能带来语义噪声，BM25 的中文分词能力也有限。需要在固定题集上比较 BM25、dense 和 hybrid，而不能只因为用了两种检索就说效果更好。

### 6. RRF 是什么，为什么不用原始分数相加

RRF 是 Reciprocal Rank Fusion，按各检索器的名次给候选打分，常见形式是各路 `1/(k + rank)` 的加和。它不直接比较 BM25 分数和 cosine 相似度，因此省去了把不同分数校准到同一尺度的步骤。

项目默认 k 为 60、检索器等权。它的代价是忽略原始分差：第一名和第二名之间差多少分不会体现在融合里。RRF 是排名融合，不是 cross-encoder reranker；当前项目没有单独的 cross-encoder 重排阶段。后续若加 reranker，应该评估它对排序质量的增益和额外延迟。

### 7. Embedding 怎么用，项目用了向量数据库吗

Embedding 模型把文本映射成向量，查询和文档使用同一模型后，可以通过向量相似度找语义相关内容。项目使用 BGE M3，但当前实际走的是 sentence-transformers 的单向量 dense 能力，向量做归一化后以点积近似 cosine。

当前没有接 FAISS 或向量数据库：离线向量存成 NumPy 分片，查询时对矩阵精确计算点积并排序。这种实现简单、容易复现，适合当前实验规模；计算要扫描向量，规模上来后会成为瓶颈。届时再比较 ANN 的召回率、延迟、更新和运维成本，而不是把目前实现描述成生产级向量服务。

### 8. Query Rewrite、HyDE 和 Multi-hop 分别是什么

Query rewrite 改写或拆解用户问题，用不同表达补充召回；HyDE 让模型生成假设性文本，再用它做检索查询；multi-hop 则针对需要多条证据的问题，先查一轮，再根据子问题或证据缺口继续查。

CompassRAG 会保留原始查询，把改写和子问题作为额外检索入口。HyDE 内容不是资料证据，最终回答仍要引用真实 chunk。multi-hop 是受轮数和预算限制的检索循环；充分度由模型判断，解析失败会记录 parsed=False 并采用偏早停降级，因此不能把它说成确定性证明。

### 9. 检索到的内容怎么进入生成和引用核验

每条证据带 source、chunk_id、quote、score、retriever 和 rank。生成时把问题和证据片段一起放进答案 Prompt，模型据此回答。启用 claim verifier 后，模型把答案拆成 claims，并标注证据编号和 supported 判断；代码校验编号是否存在、是否越界、引用是否非空。

代码验证的是引用结构合法，不会自动证明语义蕴含。语义支持还是模型判断，模型也可能误读或遇到资料冲突。当前 ResearchResult 对外默认只返回前 5 条、每条约 280 字符，而内部检索可能保留更多完整块，所以接口摘要和内部证据并不完全等价。

### 10. Hallucination 怎么处理，核验能保证答案正确吗

RAG 通过提供外部证据降低模型凭参数记忆作答的比例，但不会自动消除幻觉。模型可能没找到关键段落、误读资料、把证据外推，或遇到来源冲突。

项目把生成和核验分开：claim verifier 判断每个 claim 是否有证据支持，代码检查引用编号；Atlas 的 Auditor 检查草稿并提出 issues，必要时修订一次。它们降低了问题不可见的风险，但不构成事实正确性保证。核验开关默认关闭，解析失败会标记而非一律拒答，因此介绍时要说“提供可选核验机制”，不能说“所有答案强制通过事实验证”。

### 11. Agent、Workflow、DAG 有什么区别，Atlas 属于哪一种

Workflow 通常由程序预先定义步骤和转移；Agent 会根据观察结果选择动作或工具；DAG 是描述步骤依赖关系的一种结构，并不自动意味着自主智能。

Atlas 的研究主路径是固定模板 DAG：retrieve、draft、verify、repair、final_verify。检索内部可根据开关调用模型选择 single、multi 或 direct，并决定是否继续检索。因此更准确地说，它是带有模型决策节点的持久化研究工作流，而不是能自主拆解任意目标的通用 Agent。这个边界也让执行和恢复行为更可预测。

### 12. Function Calling 或 Tool Calling 在项目里怎么做

Tool Calling 的关键不是模型返回了函数名，而是把工具能力、参数、执行结果和失败语义纳入控制流程。模型可以提出动作，但程序还要校验参数、权限和状态，再调用工具，并把结果交回后续步骤。

Atlas 的 ResearchTool 是 Atlas 与 CompassRAG 的应用层协议：Atlas 发送 ResearchRequest，检索层返回 ResearchResult。主 DAG 的 retrieve executor 调用它，随后把结果持久化并通过 DAG data binding 传给下游。这里不要误说成所有检索都由模型原生 Function Calling 发起；顶层研究步骤是 Atlas 调度器按计划执行，模型决策主要发生在检索策略等节点。

### 13. Memory 和 RAG 有什么区别，Atlas 的图存什么

RAG 的索引主要回答“当前问题能从资料库里找哪些证据”；Memory 关注任务过程中形成的状态，例如目标、约束、决定、事实和产出，以及它们如何变化。

Atlas 的状态图节点有 Goal、Subtask、Constraint、Decision、Artifact、Fact，强调研究过程里的状态关系和有效性。它和 CompassRAG 的实体图不是同一张图：实体图把实体关系连到 chunk，用于辅助召回。并且主 DAG 的检索 refs 保存在 Attempt.output 和 ToolCall.result，不会自动全部转成 Fact。这样区分可以避免把检索索引、执行 DAG 和状态记忆都笼统叫“知识图谱”。

### 14. Prompt 的 token/context budget 怎么控制

Context window 是模型一次请求可处理的输入输出容量。控制时要给系统指令、问题、证据、历史和输出预留空间，不能只统计记忆摘要而忽略检索证据和回答。

Atlas 的 context builder 默认预算是 4000 tokens，主要针对状态视图；四区组装器按计划与约束、摘要、近期原文、提示分配上下文。API 默认通常只提供计划和提示，没有接入完整历史缓冲；问题、证据、草稿可能之后再拼进 Prompt。因此 4000 不是全请求的硬上限，ContextPackage 也记录摘要与引用元数据，不是逐字保存完整 Prompt。项目的 DAG 模型调用还没有完整接入 token/费用计量。

### 15. 怎么评测 RAG，如何证明改动有效

我把检索和生成分开评估。检索看 Recall@k、支持证据完整命中等指标；答案看 EM/F1 或人工判定；有核验时还看 unsupported claim、拒答与误拒答。运行时机制另看故障是否触发、恢复是否成功、工具是否重复调用和状态是否收敛。三类指标回答不同问题，不能用测试通过率代替答案质量。

对比 BM25、dense、hybrid 或加入 rewrite/verifier 时，固定题目、语料、模型、Prompt 和预算，保存逐题结果，再做消融。项目当前有多跳题集和受控运行时故障评测，但部分检索历史结果需要按统一协议重跑；小样本机制结果不能外推成普遍准确率提升。回答面试官时先报评测口径，再报数字。

### 16. 为什么用 SQLite 和自研运行时状态，不全交给 LangGraph

SQLite 加 SQLAlchemy 让项目可以在本地保存计划、Step、Attempt、ToolCall 和状态变化，且当前规模下部署简单。LangGraph 用于部分检索/角色控制流；Atlas 的持久化研究计划则由自己的 PlanRunner 执行，让步骤、数据绑定和恢复状态通过同一业务模型管理。

这是当前项目边界，不代表 SQLite 适合任意并发规模，也不代表自研 runner 一定优于成熟框架。多实例调度和 PostgreSQL 生产迁移尚未充分验证；技术选型要匹配规模，未来若出现更复杂的队列和并发要求，应重新评估。

### 17. 超时后为什么不能直接重试，项目怎么恢复

网络超时只说明客户端没及时拿到结果，不说明服务端一定没执行。盲目重试可能重复副作用或费用。因此 ToolCall 先持久化调用意图和稳定幂等键；如果结果未知，工具支持 lookup 时先对账并保存查回结果，不支持时就保留 unknown，必要时交人工。

恢复时从数据库读取计划和步骤，复用已经提交的输出，只继续满足条件的步骤。当前实现覆盖明确的 dispatched/对账恢复场景，不是任意代码位置崩溃都能自动接管，也不能承诺跨系统 exactly-once。

### 18. 项目的主要难点、个人贡献和下一步是什么

我会用“审核者没有拿到证据”这个案例说明工程判断：检索和草稿已经完成，但 Auditor 判答案没有依据。排查后发现不是模型能力问题，而是 DAG 只把 draft 绑定给 verify，没有把 retrieve 的 evidence 传过去。补上 retrieve 到 verify 和 final_verify 的数据绑定后，审核 Prompt 才能看到证据。这个案例说明多步系统里，信息在上游存在，不代表下游真的拿到了。

我的工作重点是检索与运行时之间的协议、研究 DAG 的步骤和数据绑定、证据核验、工具结果持久化及恢复场景。BM25、BGE M3、RRF、PPR 都是已有方法，不说成原创算法。下一步优先统一证据在 DAG 中的引用与登记、严格核验失败策略，再用固定题集做端到端消融，而不是继续堆新模块。

## 面试时的回答结构

遇到热门技术追问，可以按四句话回答：

1. **先解释概念**：它解决什么问题。
2. **再落到项目**：Atlas 的具体数据流、组件或参数是什么。
3. **解释取舍**：为什么选这个方案，代价是什么。
4. **说清边界**：哪些能力已经实现，哪些还没有验证。

回答不要背实现清单。比如问 RAG，就从资料进入、chunk、索引、召回、融合、证据进入 Prompt、生成和核验讲完整；问 Prompt，就讲本项目不同任务 Prompt 的组成和证据如何注入；问 Agent，就解释固定 DAG 与模型决策节点各自负责什么。

## 代码复习入口

| 主题 | 主要入口 |
|---|---|
| 固定研究计划与执行 | `atlas/planning/research_plan.py`、`atlas/planning/executors.py`、`atlas/planning/runner.py` |
| RAG 检索与融合 | `compassrag/retrieval/bm25.py`、`dense.py`、`fusion.py` |
| Chunk 与上传 | `compassrag/corpus/chunking.py`、`compassrag/api/ingest.py` |
| Query rewrite 与 multi-hop | `compassrag/query/rewrite.py`、`compassrag/agent/core.py` |
| Prompt 与核验 | `compassrag/agent/`、`compassrag/diagnose/verifier.py`、`atlas/planning/executors.py` |
| Tool 协议与调用恢复 | `atlas/tools/protocol.py`、`atlas/tools/compassrag.py`、`atlas/tools/dispatcher.py`、`atlas/planning/recovery.py` |
| 状态记忆与上下文 | `atlas/memory/`、`atlas/context/assembler.py`、`atlas/context/builder.py` |
| 数据模型与评测 | `atlas/storage/models.py`、`atlas/evaluation/`、`compassrag/eval/` |

### 核对记录

内容根据 2026 年 11 月 19 日工作区实现整理。后端测试结果为 953 passed、6 skipped。本文是面试准备材料，不代表本次修改项目架构或重新运行付费模型评测。代码中的选项、默认值和恢复覆盖范围可能变化，面试前应以当前配置与代码为准。
