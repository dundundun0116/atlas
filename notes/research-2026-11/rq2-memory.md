# RQ2 长期记忆与上下文管理

> 作者：rq2-memory（Atlas 调研团队）｜日期：2026-11-04
> venue 口径：一律抄 `notes/research-2026-11/venue-verified.md`（Lead 已核验）。该表未列的工作，若本轮自行核到证据则注明渠道，否则写「arXiv preprint（会议归属待核实）」。
> 证据分栏：**【论文机制】**=论文自述；**【工程实现】**=实际代码文件+具体机制；**【仅宣传】**=只有博文/README 宣称、无代码或实验支撑。

---

## 0. 结论摘要（硬结论）

1. **"长上下文""长程任务""长期记忆"是三个不同性质，混用会直接导致选错方案。** 据 [Horizon Gap](https://arxiv.org/abs/2608.06663)（arXiv 2608.06663，综述 1547 篇）：long-horizon 是**任务属性**（所需步数）、long-context 是**模型属性**（token 容量）、long-term memory 是**系统属性**（跨步/跨会话的持续性）。Atlas 的三个痛点全部落在第三类——把它们当成"上下文窗口不够大"来解，方向就错了。

2. **六类范式里，只有「结构化状态记忆」能原生表达"约束失效"和"事实冲突"；其余五类本质是 append-only 加一次再检索。** 纯历史/摘要/向量/图记忆都没有内建的 `status` 与 `supersedes` 指针：旧事实不会被标记为假，只是"这次没被检索到"。这不是实现细节，是表达能力的分界线（详见 §1、§3）。

3. **工业界向量记忆事实上已退化为 append-only。** Mem0 的 `mem0/configs/prompts.py` 同时存在两套语义：`:176` `DEFAULT_UPDATE_MEMORY_PROMPT` 声明四操作 ADD/UPDATE/DELETE/NO-CHANGE，`:264` 甚至写明"检索到的事实与记忆矛盾则删除"；但 `:468` 的 `ADDITIVE_EXTRACTION_PROMPT` 写的是 "Your **sole operation is ADD**"，而 `mem0/memory/main.py:944` 默认走后者。**【工程实现】**这说明"矛盾即删"的语义在规模化时被主动放弃了——向量库里维护唯一真源的代价太高。

4. **记忆架构的排名会随历史长度反转，只在单一 horizon 上评测的结论不可信。** [Ground Truth First](https://arxiv.org/abs/2607.21962)（arXiv 2607.21962）用带 validity interval 的事实脚本做的纵向实测：预算受限的 curated-map 内存在 3 周领先，到 9 周对**被淘汰内容**的召回从 96% 掉到 72%；而 **provenance-typed graph** 反而升到 90%，六个用户全部同向（p=0.031）。这条直接支持 Atlas 的 provenance 字段设计。

5. **写入阶段的质量是最高杠杆，远高于检索调参。** 同上工作：**写入时写得弱的事实，下游失败率 24%；写得强的只有 2%**。Atlas 的 `provenance.missing_provenance()` 门禁应该前置到写入路径，而不是只在组装时过滤。

6. **检索命中率 ≠ 任务成功率，因为 LongMemEval 的五项能力里有两项根本不测召回。** [LongMemEval](https://arxiv.org/abs/2410.10813)（ICLR 2025）的五能力是：信息抽取、多会话推理、时序推理、**知识更新**、**弃答（abstention）**。"知识更新"测的是新值覆盖旧值后模型答新值；"弃答"测的是信息不在记忆里时该说"不知道"。这两项用 recall 完全衡量不了——检索全命中但答旧值，recall=1、正确率=0。

7. **"相关信息放中间会被忽略"给四区顺序提供了实证依据。** [Lost in the Middle](https://doi.org/10.1162/tacl_a_00638)（TACL 2024）：性能在相关信息位于开头或结尾时最高，位于长上下文中段时显著下降，即便对显式长上下文模型也成立。Atlas 把"计划与约束"固定在**最前**、且用 `AuditError` 强制它不被裁掉，方向是对的。

8. **Atlas 的真正缺口不是"再加一个检索"，而是把已写好的 `supersede`/`expire` 语义接到全部真实执行路径上。** `atlas/memory/schema.py` 已有 `NodeStatus.SUPERSEDED/EXPIRED`、`superseded_by`、`valid_until`，`engine.py` 已有 `supersede()/expire()`，`assembler.py` 已有 `_audit()` 强制活跃约束/决定出现。问题在于写入端（`context/extractor.py` 的 LLM 提取）是否能可靠产生这些状态变迁——这是 Atlas 最薄的一环，也是最该做消融实验的地方。

---

## 1. 六类范式对比表

> 「约束失效」= 能否表达"这条约束/事实现在不作数了"；「事实冲突」= 两条互斥信息同时存在时能否表达"它们互相矛盾"而不是静默二选一。

| 维度 | ① 纯对话历史 | ② 摘要记忆 | ③ 向量记忆 | ④ 图记忆 | ⑤ 结构化状态记忆 | ⑥ 混合记忆 |
|---|---|---|---|---|---|---|
| **写入时机** | 每轮无条件追加 | 窗口满/边界触发，一次压缩 | 抽取后逐条 embed 写入 | 抽取实体关系后建边 | 状态**变迁时**同步写（新增/接替/过期） | 分层：热路径追加＋冷路径抽象 |
| **读取方式** | 全量拼进 prompt | 摘要＋最近 N 轮 | 相似度 top-k | 图遍历 / Personalized PageRank | 按类型与活跃度**确定性列举**（非相似度） | 检索＋状态列举并行 |
| **容量与成本** | 线性增长，最终超窗 | 有界，但信息不可逆丢失 | 有界，存储便宜、检索便宜 | 较大，建图贵（LLM 抽 OpenIE） | 很小（状态条目数≈任务复杂度） | 最高（多套设施并存） |
| **能否表达约束失效** | ✗ 只能靠"模型自己记得别用" | ✗ 摘要不承载状态 | ✗ 旧条目仍在 top-k 里 | △ 可加时间戳，但无强制语义 | **✓ 原生**：`status`＋`superseded_by`＋`valid_until` | ✓（由状态层负责） |
| **能否表达事实冲突** | ✗ 两说并存，模型各取一半 | ✗ 摘要会静默合并 | ✗ 两条都召回，靠模型自己判断 | △ 可建冲突边，需人工定 schema | **✓ 可检测并显式上报**（Atlas `ConstraintConflict`→风险区） | ✓ |
| **可审计性** | 强（原文都在） | 弱（摘要不可溯源） | 中（有 chunk id） | 中（有实体/边，缺原文） | **强**：每条带 `source/confidence/verified_at`，可回答"谁给的、多可信、何时失效" | 强 |
| **典型失败模式** | 超窗后遗忘开头；成本失控 | **关键细节在摘要中被抹掉且无人察觉** | 相似≠正确；矛盾副本共存；无版本 | 建图贵且噪声大；多跳错连 | 写入不可靠→图与事实脱节（**Atlas 的主要风险**） | 复杂度高，调试难 |
| **代表与证据** | 原始 chat log | ②MemGPT 的递归摘要、MemoryOS 的分段页 | ③Mem0（`ADDITIVE_EXTRACTION_PROMPT`）、MemoryBank | ④HippoRAG（PPR）、GraphRAG（社区摘要）、A-MEM（Zettelkasten 链接） | ⑤**Atlas 自身**、Letta memory block、Mem0 的 `DEFAULT_UPDATE_MEMORY_PROMPT`（四操作）、Memory-R1（ADD/UPDATE/DELETE/NOOP）、Ground Truth First（validity interval） | ⑥REMem（gist+fact 混合图）、MemOS（MemCube） |

**读表要点**：③向量记忆与④图记忆的差别在**索引结构**，⑤结构化状态记忆与前四者的差别在**语义**——前四类回答"哪些片段与 query 相似"，⑤回答"此刻什么为真"。这条分界线就是 §3 的核心。

---

## 2. 逐工作分析（15 字段模板）

> 依 Lead 指示压缩至最关键工作；其余以 §6 元数据表覆盖。

### Generative Agents
- 类型：论文（+ 开源实现）
- 年份：2023
- 会议或来源：Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology（UIST 2023）
- 官方链接：https://arxiv.org/abs/2304.03442 ｜ DOI 10.1145/3586183.3606763
- 主要解决的问题：让 LLM agent 在小镇沙盒里产生可信的长期行为（记得、反思、规划）
- 核心机制：memory stream（自然语言经验全量日志）+ reflection（周期性把记忆综合成更高层结论）+ 加权检索
- 关键数据结构：memory stream 条目（含时间戳、last-access、importance 分）；reflection 节点反向引用被综合的原始条目
- 执行流程：观察→写入 stream→检索（recency×importance×relevance 加权）→行动/规划；累积到阈值触发 reflection
- 如何处理失败：无显式失败处理；靠 reflection 修正行为倾向
- 如何处理状态变化：**不处理**。stream 只追加，没有"作废"概念——旧记忆只是被新记忆在打分上压过
- 如何处理记忆：全部当情景记忆（episodic）存，抽象只发生在 reflection 文本层面
- 如何评测：ablation（观察/规划/反思各去掉一个）+ 人类评估行为可信度；情人节派对涌现案例
- 真实证据：**【论文机制】**权重公式与 reflection 阈值见论文 §4；**【工程实现】**官方 repo 存在，本轮未逐文件核（GitHub API 限流）
- 对 Atlas 的可借鉴点：①**检索打分三角（新近度/重要性/相关性）**可直接用于 ②摘要区与 ③近期区的裁剪策略，替代现在的"纯最旧先删"；②reflection 的"反向引用原始条目"是摘要可溯源的雏形——Atlas 的摘要段应带上被摘要的节点 ID
- 不适合 Atlas 的地方：全量 append-only 在长程工程任务里成本不可控；没有唯一真源，无法回答"当前约束是什么"
- 可信度：**高**（ACM DOI + 完整论文 + 官方代码；机制描述与摘要一致）

### Reflexion
- 类型：论文
- 年份：2023
- 会议或来源：NeurIPS 2023（poster）
- 官方链接：https://arxiv.org/abs/2303.11366
- 主要解决的问题：不更新权重的前提下，让 agent 从试错中快速学习
- 核心机制：把任务反馈信号转成**语言化反思**，存进 episodic memory buffer，下一轮 trial 作为上下文
- 关键数据结构：episodic buffer 中的反思文本（追加，跨 trial 累积）
- 执行流程：尝试→得反馈（标量或自然语言）→自我反思写入→下一 trial 读取
- 如何处理失败：**失败是它的输入**，反思就是对失败的归因
- 如何处理状态变化：不处理。反思是叠加的，不做版本管理
- 如何处理记忆：纯情景记忆；无抽象层，无失效
- 如何评测：HumanEval 91% pass@1（对比 GPT-4 80%）、ALFWorld、HotPotQA；并做反馈类型/注入方式消融
- 真实证据：**【论文机制】**上述数字为论文自述；**【工程实现】**repo 存在（本轮未逐文件核）
- 对 Atlas 的可借鉴点：**"踩坑与提示"区（④）应当承载反思，而不只是事实罗列**。Atlas 现在 `engine.hints()` 只给"被推翻的决定+已过期约束"，缺"为什么错、下次注意什么"这层归因——这正是 Reflexion 的贡献
- 不适合 Atlas 的地方：Reflexion 的 buffer 无界增长且不分类型；对"约束已失效"毫无表达力
- 可信度：**高**（OpenReview venue + 完整实验）

### MemGPT / Letta
- 类型：论文 + GitHub 项目
- 年份：2023（论文）／持续演进
- 会议或来源：MemGPT = arXiv preprint（CoRR）；Letta = GitHub 项目
- 官方链接：https://arxiv.org/abs/2310.08560 ｜ https://github.com/letta-ai/letta
- 主要解决的问题：把上下文窗口当"虚拟内存"，用分层+换页突破窗口限制
- 核心机制：main context（in-context）vs external context（out-of-context 存储）；LLM 通过函数调用**自己编辑**自己的记忆；用 interrupt 管理控制流
- 关键数据结构：**【工程实现】**memory block。`letta/services/block_manager.py:32` 定义
  `PROMPT_AFFECTING_BLOCK_FIELDS = {"description", "label", "limit", "read_only", "value"}` ——
  即一个 block = 名字(`label`) + 内容(`value`) + **字符预算(`limit`)** + 只读保护(`read_only`) + 说明(`description`)；`:813` 明确 "ValueError if any new value exceeds its block's limit"。V1 源码在 `letta-ai/letta` 的 `archive` 分支（主仓已迁至 `letta-ai/letta-code`）。
- 执行流程：上下文压力→模型发起 memory 编辑/检索函数→工具结果回注→继续；必要时 self-edit 重写 system prompt 片段
- 如何处理失败：靠模型自己发现"记错了"再调编辑函数；无强制校验
- 如何处理状态变化：**通过覆写 block 实现**——写新值就是失效旧值，但**没有历史、没有版本、没有 supersedes 指针**
- 如何处理记忆：工作记忆（main context）+ 长期记忆（external，可检索），是 CoALA 之外最接近"分层"的工程实现
- 如何评测：文档分析（超窗文档 QA）+ 多会话对话（DMR）；对比固定窗口基线
- 真实证据：**【工程实现】**block 字段集与 limit 强制见上述文件行号；**【论文机制】**分层与 interrupt 见论文 §2–3
- 对 Atlas 的可借鉴点：**①block 的 `limit` 字段 = 给每类状态分配字符预算，这跟 Atlas 四区的 floor/share 是同一件事**，但 Letta 把它下沉到单条记忆粒度——Atlas 可以给 Constraint/Decision/Fact 各自配 per-type 预算上限，防止某一类状态挤爆计划区。②`read_only` 保护对应用户显式设定的约束（不允许 agent 自己改）
- 不适合 Atlas 的地方：block 覆写式更新**丢失版本**，无法回答"这条约束以前是什么、为什么变"；Atlas 的 `superseded_by`+`note` 比它强
- 可信度：**高**（论文 + 我实际读到代码行）

### Voyager
- 类型：论文 + GitHub 项目
- 年份：2023
- 会议或来源：TMLR（2024 接收）
- 官方链接：https://arxiv.org/abs/2305.16291
- 主要解决的问题：Minecraft 中的开放式终身学习
- 核心机制：自动课程 + **可执行代码技能库**（ever-growing skill library）+ 迭代提示（环境反馈/执行错误/自验证）
- 关键数据结构：技能 = 带描述的可执行程序 + 描述文本用于检索；按语义检索复用
- 执行流程：课程提目标→检索相似技能→写代码→执行→失败则迭代修复→成功则入技能库
- 如何处理失败：执行错误与自验证直接进下一轮 prompt
- 如何处理状态变化：**技能只增不废**；某技能被更好版本取代后旧版仍在库里
- 如何处理记忆：程序性记忆（procedural），与 CoALA 的 procedural memory 对应
- 如何评测：独特物品数 3.3×、探索距离 2.3×、科技树解锁最快 15.3×；跨世界迁移测试
- 真实证据：**【论文机制】**上述倍数为论文自述；**【工程实现】**repo 存在（本轮未逐文件核）
- 对 Atlas 的可借鉴点：**"产出物"（Artifact）应该可执行、可复用、带检索元数据**。Atlas 的 Artifact 节点目前只有 `content` 与 `未验证` 标记，缺"这个产出物在什么条件下可复用"的描述字段
- 不适合 Atlas 的地方：技能库无失效机制；Minecraft 的动作空间与长程工程任务差异大
- 可信度：**高**（TMLR + 完整代码）

### Mem0
- 类型：论文 + GitHub 项目
- 年份：2025
- 会议或来源：arXiv preprint
- 官方链接：https://arxiv.org/abs/2504.19413 ｜ https://github.com/mem0ai/mem0
- 主要解决的问题：生产环境可用的可扩展长期记忆（抽取—整合—检索）
- 核心机制：从对话中抽取显著事实，与已有记忆做**冲突消解**后写入；图变体额外建实体关系
- 关键数据结构：**【工程实现】**`mem0/memory/main.py` —— `add`(:760) → `_add_to_vector_store`(:881) → `_create_memory`(:1975) / `_update_memory`(:2052) / `_delete_memory`(:2116)；`history`(:1960) 记录变更历史，写入时带 `"event": "ADD"` 并经 `db.add_history(...)` 落历史表。
- 执行流程（关键分叉）：`main.py:944` 用 `ADDITIVE_EXTRACTION_PROMPT`，而 `configs/prompts.py:468` 该 prompt 自称 "Your **sole operation is ADD**"；另一条路径 `:176` `DEFAULT_UPDATE_MEMORY_PROMPT` 才有四操作，`:264` 写明"检索到的事实与记忆矛盾则删除"。
- 如何处理失败：无显式失败处理；抽取失败即丢
- 如何处理状态变化：**两种模式并存，默认走 append-only**。这是本轮最重要的发现之一（见 §0-3、§3）
- 如何处理记忆：语义记忆为主（抽取后的事实陈述）；图变体加关系
- 如何评测：LOCOMO 四类问题（single-hop/temporal/multi-hop/open-domain）；LLM-as-Judge；p95 延迟降 91%、token 省 >90%
- 真实证据：**【工程实现】**上述文件与行号均已实读；**【论文机制】**LOCOMO 数字为论文自述
- 对 Atlas 的可借鉴点：**`history` 表 + event 类型是"可审计写入"的最小实现**，Atlas 的 event log 应对齐这个粒度：每次状态变迁记 (memory_id, old, new, event, created_at)
- 不适合 Atlas 的地方：默认 append-only 恰恰是 Atlas 要避免的失败模式——"矛盾副本共存，靠检索撞运气"。Atlas 不能照抄它的默认路径，只能照抄它的 `history` 表设计
- 可信度：**高**（代码已实读，机制可直接验证）

### MemoryBank
- 类型：论文
- 年份：2023/2024
- 会议或来源：AAAI 2024
- 官方链接：https://arxiv.org/abs/2305.10250 ｜ DOI 10.1609/aaai.v38i17.29946
- 主要解决的问题：长期陪伴型 chatbot 的记忆与人格适配
- 核心机制：受**艾宾浩斯遗忘曲线**启发的记忆更新——按时间间隔与记忆相对重要性做遗忘与强化
- 关键数据结构：记忆条目带强度/权重，随时间衰减，被访问则强化
- 执行流程：对话→抽取记忆→按曲线衰减→检索时强度与相关性共同决定
- 如何处理失败：遗忘即失败处理（低价值记忆自然消失）
- 如何处理状态变化：**只有衰减，没有作废**。重要但已失效的记忆可能因高强度一直留存——这是它的结构性缺陷
- 如何处理记忆：情景＋语义混合，但统一建模为"强度"
- 如何评测：SiliconFriend 陪伴场景；真实用户对话定性 + 模拟对话定量
- 真实证据：**【论文机制】**遗忘曲线机制见论文；**【工程实现】**未见官方完整 repo
- 对 Atlas 的可借鉴点：**"强度/衰减"可作为④踩坑区的排序信号**——被反复踩到的坑应该更靠前，而不是按时间最旧先删
- 不适合 Atlas 的地方：**衰减曲线与"事实失效"是正交的两件事**。一条约束不会因为"很久没提"就不再是约束；把它衰减掉就是 bug。Atlas 必须用 `status` 而非权重表达失效
- 可信度：**高**（AAAI 2024 正式录用）

### HippoRAG
- 类型：论文 + GitHub 项目
- 年份：2024
- 会议或来源：NeurIPS 2024
- 官方链接：https://arxiv.org/abs/2405.14831 ｜ https://github.com/OSU-NLP-Group/HippoRAG
- 主要解决的问题：RAG 难以在大量新经验上做深度整合（多跳）
- 核心机制：受海马体索引理论启发，LLM + 知识图 + **Personalized PageRank** 协同：图做"索引"，PPR 做"扩散检索"
- 关键数据结构：**【工程实现】**`src/hipporag/HippoRAG.py`：`index`(:499)、`delete`(:595)、`_remove_fact_sources_from_graph`(:684)、`retrieve`(:704)、`rerank_facts`；`src/hipporag/embedding_store.py` 的 `BaseEmbeddingStore` 提供 insert/delete/get_row 的 chunk/fact 存储。repo 4098 stars（本机经 api.github.com 读到）。
- 执行流程：OpenIE 抽三元组→建实体图（passage 节点与 entity 节点两类）→query 抽实体作种子→PPR 扩散得候选段落→rerank
- 如何处理失败：无显式失败处理；多跳错连靠 rerank 兜
- 如何处理状态变化：**部分支持**——`delete(docs_to_delete)` 与 `_remove_fact_sources_from_graph` 说明它支持按来源删除事实，但这是"删数据"不是"标记失效"：删掉后无法回答"曾经有什么"
- 如何处理记忆：语义记忆的图索引；不做情景/程序性区分
- 如何评测：多跳 QA（2WikiMultiHopQA、HotpotQA、MuSiQue）提升至多 20%；单步检索媲美 IRCoT 且便宜 10–30×、快 6–13×
- 真实证据：**【工程实现】**上述文件与行号已实读；PPR 算法本身在论文摘要中明确声明，但**具体实现文件我未定位到**（`src/hipporag/` 下探测的多条路径 404），标「待核实」
- 对 Atlas 的可借鉴点：**图遍历可以作为"状态邻域扩展"的读取方式**，而不只是相似度。Atlas 的 `dependency_order()` 只是拓扑排序；用 PPR 从当前 focus 节点扩散，可召回"与当前子任务间接相关的历史决策"——这是纯 top-k 拿不到的
- 不适合 Atlas 的地方：HippoRAG 的图节点是**从文本抽的三元组**，不是受管状态；没有 `status`，没有唯一真源，无法支撑 `_audit()`
- 可信度：**中高**（NeurIPS 2024 + 代码实读；PPR 实现细节待核实）

### GraphRAG
- 类型：论文
- 年份：2024
- 会议或来源：arXiv preprint
- 官方链接：https://arxiv.org/abs/2404.16130
- 主要解决的问题：RAG 在**全局性**问题（"这批文档的主题是什么"）上失效——那是 query-focused summarization 而非检索
- 核心机制：LLM 两阶段建图：①从文档抽实体知识图；②为紧密关联实体组**预生成社区摘要**；查询时每个社区摘要出部分答案，再汇总
- 关键数据结构：实体图 + 社区层级 + 社区摘要（预生成、缓存）
- 执行流程：建图→社区检测→每社区摘要→query→各社区部分答案→map-reduce 汇总
- 如何处理失败：无显式失败处理
- 如何处理状态变化：**不处理**，索引是静态快照
- 如何处理记忆：全局语义摘要层
- 如何评测：百万 token 级数据集上的全局 sensemaking 问题；比传统 RAG 在 comprehensiveness/diversity 上更好
- 真实证据：**【论文机制】**两阶段与社区摘要见论文；**【工程实现】**微软 graphrag 库存在（本轮未逐文件核）
- 对 Atlas 的可借鉴点：**"预生成摘要"这个思路可搬到②摘要区**——不要每次结转都重新摘要整段历史，而是维护分层摘要（子目标级→阶段级），查询时按需拼接。这直接降低结转成本
- 不适合 Atlas 的地方：为"全局主题理解"设计，不关心单条约束的活跃与否；建图与社区摘要成本高，Atlas 的任务规模不划算
- 可信度：**高**（论文完整、方法清晰）

### LongMemEval
- 类型：论文 + benchmark
- 年份：2024/2025
- 会议或来源：ICLR 2025
- 官方链接：https://arxiv.org/abs/2410.10813 ｜ https://github.com/xiaowu0162/LongMemEval
- 主要解决的问题：长程交互中记忆能力的系统评测（此前无基准）
- 核心机制：500 道人工精编问题嵌入可自由伸缩的对话历史；把记忆设计拆成**三阶段**：indexing / retrieval / reading
- 关键数据结构：五类问题各自带标注答案与证据会话；历史长度可参数化
- 执行流程：注入历史→按被测系统的索引/检索/读取跑→判分
- 如何处理失败：**"弃答"能力本身就是失败处理的评测**——信息不存在时该 abstain
- 如何处理状态变化：**"知识更新"能力专测这个**：用户先说 A 后改说 B，问 B
- 如何处理记忆：不规定实现，只规定能力面
- 如何评测：五能力——信息抽取、多会话推理、时序推理、知识更新、弃答；商业助手与长上下文 LLM 在持续交互上掉 30% 准确率；三项优化（session decomposition、fact-augmented key expansion、time-aware query expansion）
- 真实证据：**【论文机制】**五能力与三阶段见论文摘要原文
- 对 Atlas 的可借鉴点：**五能力可直接改写为 Atlas 的评测矩阵**（见 §5）；特别是"知识更新"与"弃答"两项，正是 Atlas 图记忆最该证明自己的地方
- 不适合 Atlas 的地方：面向对话助手（用户偏好类记忆），不是工程任务的约束/产出物；历史是"对话"不是"工具 trace"
- 可信度：**高**（ICLR 2025 + 公开 benchmark）

### CoALA（Cognitive Architectures for Language Agents）
- 类型：论文
- 年份：2023/2024
- 会议或来源：TMLR
- 官方链接：https://arxiv.org/abs/2309.02427
- 主要解决的问题：缺乏统一框架来组织和比较 language agent
- 核心机制：从认知科学与符号 AI 借来模块化记忆 + 结构化动作空间 + 广义决策过程
- 关键数据结构：**记忆二分**——working memory（当前决策周期内）vs long-term memory；长期再分 **episodic（经历）/ semantic（事实）/ procedural（技能）**
- 执行流程：决策循环：规划→在内部记忆上执行→在外部环境执行→观察→更新工作记忆
- 如何处理失败：框架层面不处理，靠动作空间里的反思动作
- 如何处理状态变化：working memory 每步重写；long-term 追加
- 如何处理记忆：上述四类，是本文最主要的贡献
- 如何评测：回顾性组织大量已有工作 + 前瞻性指出方向（框架论文，无新实验）
- 真实证据：**【论文机制】**四类记忆划分见论文；venue=TMLR（Lead 表已核）
- 对 Atlas 的可借鉴点：**这是回答"工作/长期/情景/语义记忆区别"最权威的框架**（见 §3 开头）。Atlas 的四区可精确映射：③近期原始记录=working memory；②已完成摘要=episodic 摘要；①计划与约束中的 Constraint/Fact=semantic；⚠️**Atlas 缺 procedural memory**——`Artifact` 只存产出物内容，不存"怎么做到的"，这是明确的功能缺口
- 不适合 Atlas 的地方：描述性框架，不给实现；对"失效/冲突"没有规定
- 可信度：**高**（TMLR + 被广泛引用为该领域标准框架）

### A-MEM
- 类型：论文
- 年份：2025
- 会议或来源：NeurIPS 2025
- 官方链接：https://arxiv.org/abs/2502.12110
- 主要解决的问题：现有记忆系统只有基础存取，缺"组织"；固定操作与结构限制跨任务适应性
- 核心机制：Zettelkasten 方法——新记忆生成含上下文描述/关键词/标签的结构化笔记，再分析历史记忆建立链接；**memory evolution**：新记忆写入会触发对已有记忆的上下文表示与属性更新
- 关键数据结构：笔记（note）＝内容＋context＋keywords＋tags＋链接集合
- 执行流程：写入→生成结构化属性→检索历史相似→建边→**反向更新旧笔记属性**
- 如何处理失败：无显式失败处理
- 如何处理状态变化：**有"演化"但无"作废"**——旧笔记被改写属性，但不会标记为假。这是它与状态记忆的关键差别
- 如何处理记忆：语义/情景混合的图，强调互联
- 如何评测：六个基座模型上对比 SOTA；未见大规模长程任务评测
- 真实证据：**【论文机制】**机制见摘要；**【工程实现】**两个 repo（A-mem / A-mem-sys）存在，本轮未逐文件核
- 对 Atlas 的可借鉴点：**"写入时反向更新旧条目"是 Atlas 的 `supersede()` 该做的事**，但 A-MEM 只更新表示、Atlas 要更新**真值状态**。A-MEM 的字段设计（context/keywords/tags）可补进 Atlas 的 `Node.meta`
- 不适合 Atlas 的地方：无 `status`、无版本、无审计链；"演化"是表示层漂移，不是真值变迁
- 可信度：**高**（NeurIPS 2025）

### Memory-R1
- 类型：论文
- 年份：2025
- 会议或来源：arXiv preprint
- 官方链接：https://arxiv.org/abs/2508.19828
- 主要解决的问题：现有记忆管线是静态、启发式的，**缺"该存/该改/该删/该取"的学习机制**
- 核心机制：两个 RL 训练的 agent——Memory Manager 学**结构化操作 ADD / UPDATE / DELETE / NOOP**，Answer Agent 预选条目再推理；PPO/GRPO，仅 152 训练 QA 对
- 关键数据结构：记忆条目 + 操作集（四类显式状态转移）
- 执行流程：交互→Memory Manager 决定四操作之一→Answer Agent 选条目→作答→结果驱动 RL
- 如何处理失败：NOOP 是"不需记忆动作"的显式出口；策略由结果优化
- 如何处理状态变化：**四操作就是状态机**，是本清单里除 Mem0 旧 prompt 外最明确的"记忆当状态管"的论文
- 如何处理记忆：统一条目，按需增改删
- 如何评测：LoCoMo、MSC、LongMemEval 三个 benchmark，3B–14B 多尺度；超强基线
- 真实证据：**【论文机制】**四操作与 RL 设置见摘要原文
- 对 Atlas 的可借鉴点：**把 Atlas 的写入动作显式化为四操作，并用结果来校准**。Atlas 目前是 extractor 一次性写图、无 NOOP、无反馈闭环——加 NOOP 能显著减少噪声写入（对应 §0-5 的"写前校验"）
- 不适合 Atlas 的地方：需要训练；Atlas 若不想训，可先抄它的**动作空间**（ADD/UPDATE/DELETE/NOOP）而不抄 RL
- 可信度：**中**（arXiv，无会议归属证据；实验自述，未复现）

### REMem
- 类型：论文
- 年份：2026
- 会议或来源：arXiv preprint
- 官方链接：https://arxiv.org/abs/2602.13530
- 主要解决的问题：**现有 agent 记忆几乎全是语义的，缺情景记忆**；现有工作"忽视情景性、缺显式事件建模、过度强调简单检索而非复杂推理"
- 核心机制：两阶段——①离线索引：把经历转成**混合记忆图，把 time-aware gist 与 fact 灵活连起来**；②在线推理：带工具的 agentic retriever 在图上迭代检索
- 关键数据结构：混合记忆图，节点分 gist（时间感知的情节摘要）与 fact（事实）；事件建模显式
- 执行流程：经历→切事件→生成 gist+fact→建图→查询时 agentic 多轮检索→推理
- 如何处理失败：更强的"拒答"（refusal）行为
- 如何处理状态变化：有事件时间线，但未声明失效/版本语义
- 如何处理记忆：**明确区分情景与语义并在同一图里共存**——与 Atlas 的多类型节点同构
- 如何评测：四个情景记忆 benchmark；比 Mem0、HippoRAG 2 在情景回忆 +3.4%、推理 +13.4%（绝对）
- 真实证据：**【论文机制】**见摘要原文
- 对 Atlas 的可借鉴点：**gist/fact 二分可以补 Atlas 的④区**——`hints()` 现在是事实罗列，缺"当时那个情境"的 gist。给 hint 加时间与情境标签，能显著提升可迁移性
- 不适合 Atlas 的地方：面向对话式情景回忆，不面向工程约束管理
- 可信度：**中**（arXiv，Yu Su 组工作，方法可信但未复现）

### Ground Truth First（记忆评测方法论）
- 类型：论文（评测工具 + 开源库 Veracium）
- 年份：2026
- 会议或来源：arXiv preprint
- 官方链接：https://arxiv.org/abs/2607.21962
- 主要解决的问题：现有记忆 benchmark **先造对话再抽答案**（有标签错误与污染），且历史太短
- 核心机制：**反转流水线**——先用 seed life-script sampler 生成带 **validity interval、volatility class、source channel** 的事实，再渲染成对话/邮件，再机械生成问题，保证 gold answer 按脚本为真
- 关键数据结构：fact + `validity interval` + `volatility class` + `source channel` + per-event fact manifest（**这是本轮见到的最完整的字段级"状态事实"设计**）
- 执行流程：脚本→渲染→保真校验→问题实例化→跨架构评测（固定 answerer、版本化 LLM judge、3 次重复、2 个 horizon）
- 如何处理失败：注入探针（injection probes）测 provenance 边界是否存活
- 如何处理状态变化：**as-of-date 问题集**——问"在某日期当时为真的事实"，强制系统正确处理有效期
- 如何处理记忆：把记忆架构分五类横评
- 如何评测：约 380 题、15 种类型；关键发现见 §0-4、§0-5
- 真实证据：**【论文机制】**字段设计与数字见摘要原文
- 对 Atlas 的可借鉴点：**这是 Atlas 最该对标的评测范式**。它的 fact 字段（validity interval / volatility class / source channel）与 Atlas 的 `valid_from/valid_until/source` 几乎一一对应——Atlas 可以照这个思路自建"约束有效期 + 冲突"评测集，而不必依赖对话式基准
- 不适合 Atlas 的地方：语料是合成的生活脚本（邮件/聊天），非工程任务；需自建等价物
- 可信度：**中高**（方法与开源库清晰，单作者、未复现，但结论方向与其他工作一致）

---

## 3. 状态记忆 vs 检索记忆（核心区分 + 证据）

### 3.1 先厘清记忆分类（回答 RQ2 的分类问题）

按 [CoALA](https://arxiv.org/abs/2309.02427)（TMLR）的权威划分：

| 类别 | 定义 | 生命周期 | Atlas 对应 | 现状 |
|---|---|---|---|---|
| **工作记忆** | 当前决策周期内的信息 | 每步重写 | ③近期原始记录 + 组装后的 prompt | ✅ 有 |
| **情景记忆** | 具体经历（何时何地发生什么） | 跨会话留存 | ②已完成摘要 + event log | ✅ 有（但摘要丢了情境细节） |
| **语义记忆** | 去情境的事实与约束 | 长期，需版本 | ①Constraint / Fact / Decision | ✅ 有（最强的一区） |
| **程序性记忆** | 怎么做事（技能/流程） | 长期，可复用 | ❌ **缺**（Artifact 只存结果） | ⚠️ 缺口 |

**关键判断**：Atlas 的"图状态记忆"命中语义记忆这一格，且带版本；RAG 命中语义记忆的**另一种实现**（相似度索引），但不带版本。二者不是替代关系，是同一格里的两种精度。

### 3.2 两种东西的分界线

| | **状态记忆（state）** | **检索记忆（retrieval）** |
|---|---|---|
| 回答的问题 | 当前任务此刻**为真**的事实/约束是什么 | 哪些外部文本片段与当前 **query 相似** |
| 真值假设 | **唯一真源**（同一 key 只应有一个活跃值） | 无真值概念，多副本共存 |
| 更新语义 | 就地变迁：`supersede` / `expire` / 版本指针 | 追加；旧条目仍在索引里 |
| 失效表达 | 一等公民：`status`、`valid_until`、`superseded_by` | **无法表达**，只能靠时间戳旁证 |
| 冲突处理 | 检测并显式上报（交人裁决或生 Decision 节点） | 两条一起召回，静默交给模型 |
| 读取方式 | **确定性列举**（按类型/活跃度），同一快照必得同一结果 | 概率性 top-k，受 embedder/索引影响 |
| 可复现性 | 高（纯函数、可快照） | 低（索引漂移即结果漂移） |
| 成本结构 | 写入贵（要校验/去重/判断冲突），读取极便宜 | 写入便宜，每次查询都要 embed + 检索 |
| 失败模式 | 写入不可靠→图与事实脱节 | 矛盾副本共存→模型随机采信 |

### 3.3 支持"当状态管"的已发表证据（≥3）

1. **Mem0 的四操作 prompt（论文+代码）**——`configs/prompts.py:176` 声明 ADD/UPDATE/DELETE/NO-CHANGE，`:264` 明写"矛盾则删除"。这是**工业系统自己承认需要状态语义**的证据；而它默认改走 `:472` "sole operation is ADD"（`main.py:944`），恰好说明**在规模化压力下放弃状态语义会退化**。正反两面都在同一份代码里，是最硬的证据。
2. **Memory-R1（arXiv 2508.19828）**——把记忆管理显式化为 `ADD / UPDATE / DELETE / NOOP` 四操作状态机，用结果驱动 RL 学习何时迁移。论文明确指出既有管线"静态、启发式驱动，缺乏决定该存/改/删/取的学习机制"。
3. **Ground Truth First（arXiv 2607.21962）**——字段级给出 `validity interval` + `volatility class` + `source channel`，并用 **as-of-date 问题集**强制系统正确处理有效期。实测证明：带 provenance 类型的图在长 horizon 上**反超**预算受限的 curated-map（72% → 90%），且写入质量直接决定下游（24% vs 2% 失败率）。
4. **Atlas 自身的 `provenance.py`**——`PROVENANCE_FIELDS = (source, actor, confidence, valid_from, valid_until, verified_at)`、`PROVENANCE_REQUIRED_TYPES = (FACT, CONSTRAINT, DECISION, ARTIFACT)`、`constraint_key()/ConstraintConflict`、`_EXCLUSIVE_KEY_FIELDS = ("key","topic","subject")`。这套设计与 Ground Truth First 的 fact 字段几乎同构，方向被独立工作印证。

### 3.4 支持"当检索管"的对照证据（≥2）

1. **HippoRAG（NeurIPS 2024）**——图只是**索引结构**：PPR 给候选段落打分。它的 `delete(docs)` 是删数据，不是标记失效；删后无法回答"曾经有什么"。**图记忆 ≠ 状态记忆**，这是最常见的混淆点。
2. **GraphRAG（arXiv 2404.16130）**——社区摘要预生成，本质是"把全局摘要当成可检索对象"，索引是静态快照，无状态变迁。
3. **A-MEM（NeurIPS 2025）**——有 memory evolution（新记忆反向更新旧笔记属性），但更新的是**表示**（context/keywords/tags），不是**真值**；没有 `status`、没有版本链。这是"看起来像状态、其实是表示漂移"的典型。
4. **MemoryBank（AAAI 2024）**——遗忘曲线决定留存。但"很久没提"与"不再为真"正交：一条约束不会因衰减而失效。**用权重表达失效是类别错误**。

### 3.5 结论

**图记忆和状态记忆是正交的两个维度**：HippoRAG/A-MEM/GraphRAG 证明了"图"可以作为**索引**；Mem0 四操作、Memory-R1、Ground Truth First 证明了"状态"需要**显式操作与版本**。Atlas 走的是"图 + 状态"的组合，这个组合在文献里最接近 Ground Truth First 的 provenance-typed graph 与 REMem 的混合图——**方向有独立证据支持，不是自创**。Atlas 需要防的是把图退化成索引（只做相似度召回，不维护 `status`），那就会退回到 HippoRAG 那一格，失去唯一真源。

---

## 4. 四区上下文吸收方案

> 逐区给：放什么 / 预算 / 写入时机 / 写入前校验 / 失效与冲突 / 防"检索命中当状态真值"。全部对照 `atlas/context/assembler.py`、`atlas/context/carryover.py`、`atlas/context/provenance.py`、`atlas/memory/engine.py` 的现有实现。

### 区① 当前计划与约束（`ZONE_PLAN`）
- **放什么**：Goal → open Subtasks（`dependency_order()` 依赖序）→ 活跃 Constraint → 活跃 Decision（带 `reason`）→ 环境 Fact → Artifact（带"未验证"标记）→ `provenance.risk_lines()` 风险段。**保持现有顺序**，因为 Lost in the Middle 证明开头位置可信度最高，而计划区正是"必须被遵守"的内容。
- **预算**：**不平分，先占先得**——现有实现是"计划区先全额，剩余再分配"，正确，保留。建议加 **per-type 上限**（借 Letta block 的 `limit` 思想）：Constraint 不超过计划区 40%、Decision 40%、Fact 20%。防止某一类状态（尤其 Fact）挤爆其余。超出时不要静默裁，抛 `PlanZoneOverflow`（现有行为）。
- **写入时机**：**状态变迁时同步写**，即在 `CarryOverManager.execute()` 的第一步（extractor 入图）里完成；绝不在摘要生成之后补写。现有 `carryover.py` 的"先 extractor 再摘要"顺序是对的。
- **写入前校验**（当前缺口）：在 `engine._add()` 已有"content 非空"与"Decision 必须带 reason"之外，增加：
  1. `provenance.missing_provenance()` 门禁——FACT/CONSTRAINT/DECISION/ARTIFACT 缺 `source` 或 `confidence` 时**拒绝入图**，转进候选区；
  2. 同 key 冲突检测：写入前用 `constraint_key()` 查活跃同 key 节点，若内容互斥则**不覆盖**，而是新建节点并把两者都标 `meta["conflict_with"]`；
  3. **NOOP 出口**（借 Memory-R1）：extractor 允许输出"本轮无状态变化"，避免为凑数写噪声节点。
- **失效与冲突**：沿用 `supersede()` / `expire()`；冲突经 `ConstraintConflict` 进 `risk_lines()`，**绝不静默挑一条**。新增一条硬规则：**`expire()` 只能由工具结果或用户显式指令触发，禁止 LLM 从摘要里自行推断"这条过期了"**。
- **防检索污染**：**CompassRAG 的命中永远不能直接进区①**。检索结果先进 `candidate` 暂存区（可放区④下方或独立附录），经上述写入校验后才可能成为 Fact 节点。

### 区② 已完成工作摘要（`ZONE_SUMMARY`）
- **放什么**：**只放叙事，不放状态**（现有设计正确，`carryover.py` 注释已写明"摘要只管叙事，状态在图里"）。补两点：①每条摘要标注**被摘要的节点 ID 列表**（借 Generative Agents reflection 的反向引用），使摘要可溯源；②子目标边界标注保留（现已实现，只报一次）。
- **预算**：`DEFAULT_SHARES["summary"]=0.30`、`floor=300`。建议**动态**：刚结转完时区③为空，把份额临时让给区②；接近 80% 阈值时反向。这与 `CarryOverTrigger` 的状态天然对齐，实现成本低。
- **写入时机**：结转时（子目标边界或 80% 阈值），**在 extractor 之后**。
- **写入前校验**：摘要不得引入原文没有的状态断言。可做机械检查：摘要中出现的 `[C#]/[D#]` ID 必须真实存在于图中；出现的数字/路径必须能在 `recent_raw` 里找到子串。
- **失效与冲突**：摘要本身不做失效（它不该承载状态）。但当某条摘要所引用的约束后来被 `supersede`，摘要不重写——因为状态真值不在摘要里，这是设计上的优势，要守住。
- **裁剪策略改进**：现在是 `_fit_items()` 从头部 pop（最旧先删）。建议改为**三段打分**：被活跃约束/决定引用的摘要段**钉住不删** → 其次是 recall 过的 → 最后才按时间。对应 Generative Agents 的 importance 维度。

### 区③ 近期原始记录（`ZONE_RECENT`）
- **放什么**：**逐字保留的原始 trace**（这区是工作记忆，不能摘要化）。建议分层：完整保留最后 N 条 + 仅保留"高信息密度行"（工具报错、文件路径、命令、退出码），其余压成一行。
- **预算**：`shares=0.55`、`floor=400`——占比最高是合理的（在 80% 阈值触发前它就是主战场）。但建议随结转相位动态调整（见区②）。
- **写入时机**：每步无条件追加（append-only），这是唯一不需要校验的区。
- **写入前校验**：不需要内容校验；但要保证**工具 trace 带 provenance**（哪个工具、什么参数、什么返回），供后续 extractor 生成 `source` 字段。
- **失效与冲突**：不适用——原始记录只进不出，失效由区①的状态节点表达。
- **关键纪律**：**区③可以包含互相矛盾的记录，这是它的价值**（保留了"当时确实观察到 X"）。矛盾的解释权属于区①。

### 区④ 踩坑与提示（`ZONE_HINTS`）
- **放什么**：`engine.hints()` 现给"被推翻的决定+已过期约束"。建议扩为三类：
  1. **失效状态 + 原因 + 接替者**：不只说"F3 已过期"，要说"F3（旧值 X）已被 F7（新值 Y）接替，原因：工具返回 Y"——借 REMem 的 gist 思想补情境；
  2. **反思性归因**（借 Reflexion）：为什么错、下次注意什么，而不只是"这条死了"；
  3. **冲突未决项**：从 `risk_lines()` 摘出的待裁决冲突。
- **预算**：`shares=0.15`、`floor=120`，从头部裁（最旧先去）。建议改为按**强度**排序（借 MemoryBank 的强化思想：被反复踩到的坑优先），而非纯时间。
- **写入时机**：与区①同步（`supersede`/`expire` 时自动产生），不额外写。
- **写入前校验**：hint 必须能指回具体节点 ID，否则丢弃。
- **失效与冲突**：hint 本身永久有效（历史事实），但可被"同一坑的新 hint"合并去重。
- **防误用**：hint 必须明确写"**已失效，勿用**"，避免模型把历史错误当现行规则。这是当前 `hints()` 输出最需要加固的措辞。

### 4.5 跨区三条硬规则（防"检索命中当状态真值"）
1. **写入闸门唯一**：任何外部文本（CompassRAG、网页、文档）进入区①的唯一通道是"候选 → 校验（source/confidence/有效期）→ 状态节点"。检索器**没有直接写图权限**。
2. **读取通道分离**：区①只做**确定性列举**（按类型+活跃度），永不接受相似度排序；相似度只能用于区③④的**补充**。
3. **真值唯一性检查**：`_audit()` 已保证活跃约束/决定必须出现；补一条"同 key 活跃节点超过 1 个且未标 conflict 即报错"，把静默二选一变成硬失败。

---

## 5. 记忆评测设计与消融

### 5.1 LongMemEval 到底测什么
- **五项能力**：①信息抽取（从长历史里取出一条具体信息）②多会话推理（跨多次会话组合）③时序推理（事件先后/时间点）④**知识更新**（旧值被新值覆盖后答新值）⑤**弃答**（信息不存在时不说瞎话）。
- **三阶段拆解**：indexing（怎么切分与索引）/ retrieval（怎么找）/ reading（找到后怎么读）。这是它最有方法论价值的部分——**把"记忆"这个黑箱拆成三个可分别优化的环节**。
- **规模**：500 题，历史长度可参数化；商业助手与长上下文 LLM 在持续交互上掉约 30%。

### 5.2 为什么检索命中率 ≠ 任务成功率
1. **两个能力不测召回**：知识更新与弃答，用 recall 衡量恒为满/无关。检索全命中但答旧值 → recall=1、正确率=0。
2. **命中≠被使用**：Lost in the Middle 证明相关信息在长上下文中段会被忽略——检索到了、也塞进去了，模型还是没用上。
3. **命中≠更新**：检索式记忆里新旧两条都能被召回，模型有 50% 概率采信旧值；此时"命中率"甚至因多召回一条而**上升**。
4. **单 horizon 结论会反转**：Ground Truth First 显示 3 周与 9 周的架构排名相反。
5. **写入质量是上游瓶颈**：写得弱的事实下游失败率 24%（vs 2%），检索再怎么调都补不回来。

### 5.3 什么算"记忆有帮助"
建议用**任务层指标为主、记忆层指标为诊断**：
- 主指标（任务层）：任务成功率、**约束违反次数/千步**、**使用已失效事实次数**、重复已完成子任务次数、中断后恢复成功率、每成功步的 token 成本。
- 诊断指标（记忆层）：检索 recall@k、状态节点准确率（图中活跃状态与真实环境的吻合度）、写入噪声率（被后续 supersede 的节点占比）、审计通过率。
- **判定"有帮助"的操作化定义**：在**相同 token 预算**下，开记忆相对关记忆的任务成功率提升 ≥ X%，且约束违反率下降 ≥ Y%，并且在**长 horizon 桶**里优势不缩水（不出现 Ground Truth First 那种反转）。

### 5.4 可操作消融（6 组）
| 组 | 配置 | 要回答的问题 |
|---|---|---|
| A | 全量 Atlas（图 + 四区 + provenance + audit） | 上限 |
| B | **关图记忆**：不建状态节点，只留区②③④ | 图记忆的净贡献 |
| C | **关摘要**：区②置空，只留区①③④ | 摘要是否在丢关键细节（对照 §5-2） |
| D | **只留最近 N 轮**：区②④全关，区①只留 Goal，N 扫 {5,10,20} | 最朴素基线的追赶曲线 |
| E | **RAG-only**：CompassRAG 当纯工具，无状态真源 | 检索能否替代状态 |
| F | **关 provenance**：去掉 source/confidence/valid_until 与冲突检测 | 失效/冲突语义的净贡献 |

**必做的三个附加维度**：
- **horizon 分桶**（短/中/长，对应 Ground Truth First 的 3 周 vs 9 周）：必须分桶报，禁止只报总平均。
- **写入质量分桶**：人为注入弱写入（缺 source、含糊措辞），验证 24% vs 2% 的失败率差异是否在 Atlas 上复现。
- **冲突注入**：在任务中途给一条与原约束矛盾的新约束，看 A 组是否**显式上报冲突**（进 risk 区）而不是静默选一条。这是 Atlas 最有区分度的测试，现有基准都没有。

### 5.5 数据来源建议
不建议直接跑对话式基准（LongMemEval/LOCOMO）当主评测——它们是用户偏好记忆，与工程任务不同构。建议：
1. **主评测**：自建"长程工程任务 + 中途约束变更 + 环境事实失效"任务集，照 Ground Truth First 的"先写带有效期的脚本、再渲染成 trace、再机械生成问题"流程，保证 gold 由脚本构造。
2. **外部可比性**：在 LongMemEval 上只跑"知识更新"与"弃答"两个子集，作为外部锚点。
3. **成本**：记录每成功步 token，作为与 RAG 对比的核心卖点（Mem0 用 p95 延迟与 token 省 90% 作为卖点，Atlas 应有同类数字）。

---

## 6. 元数据核验表

> venue 栏抄 `venue-verified.md`；带 ★ 的是本轮我自行核到、与 Lead 表不同或表内未收录的，已在「核验方式」注明渠道，建议 Lead 复核后并入总表。

| 简称 | 标题 | 年份 | venue（工具原样） | 链接 | 核验方式 |
|---|---|---|---|---|---|
| Generative Agents | Generative Agents: Interactive Simulacra of Human Behavior | 2023 | ★ Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology | https://arxiv.org/abs/2304.03442 | ★ Crossref `?mailto=`：DOI 10.1145/3586183.3606763，container-title 与 type=proceedings-article（**Lead 表标「待核实」，此条可补**） |
| Reflexion | Reflexion: Language Agents with Verbal Reinforcement Learning | 2023 | NeurIPS 2023（poster） | https://arxiv.org/abs/2303.11366 | Lead 表；本轮 OpenReview 复现同一 venue |
| MemGPT | MemGPT: Towards LLMs as Operating Systems | 2023 | arXiv preprint（CoRR） | https://arxiv.org/abs/2310.08560 | Lead 表；本轮 OpenReview 返回「CoRR 2023」一致 |
| Letta | （GitHub 项目） | — | GitHub 项目 | https://github.com/letta-ai/letta | 本轮实读 `archive` 分支 `letta/services/block_manager.py`（主仓已迁至 letta-ai/letta-code） |
| Voyager | Voyager: An Open-Ended Embodied Agent with Large Language Models | 2023 | TMLR（2024 接收） | https://arxiv.org/abs/2305.16291 | Lead 表（OpenReview「Accepted by TMLR」） |
| Mem0 | Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory | 2025 | arXiv preprint | https://arxiv.org/abs/2504.19413 | Lead 表；本轮实读 `mem0/memory/main.py` 与 `mem0/configs/prompts.py` |
| MemoryBank | MemoryBank: Enhancing Large Language Models with Long-Term Memory | 2023/2024 | AAAI 2024 | https://arxiv.org/abs/2305.10250 | Lead 表；本轮 AAAI OJS `citation_journal_title=Proceedings of the AAAI Conference on Artificial Intelligence`、vol 38 一致 |
| HippoRAG | HippoRAG: Neurobiologically Inspired Long-Term Memory for Large Language Models | 2024 | NeurIPS 2024 | https://arxiv.org/abs/2405.14831 | Lead 表；本轮 OpenReview 返回「NeurIPS 2024 poster」一致；代码已实读 |
| GraphRAG | From Local to Global: A Graph RAG Approach to Query-Focused Summarization | 2024 | arXiv preprint | https://arxiv.org/abs/2404.16130 | Lead 表 |
| LongMemEval | LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory | 2024/2025 | ICLR 2025 | https://arxiv.org/abs/2410.10813 | Lead 表（arXiv Comments） |
| A-MEM | A-MEM: Agentic Memory for LLM Agents | 2025 | NeurIPS 2025 | https://arxiv.org/abs/2502.12110 | Lead 表（arXiv Comments） |
| MemOS | MemOS: A Memory OS for AI System | 2025 | arXiv preprint | https://arxiv.org/abs/2507.03724 | Lead 表。⚠️ 另有 arXiv 2505.22101「MemOS: An Operating System for Memory-Augmented Generation (MAG) in LLMs」（Zhiyu Li 等），**标题不同、非同一篇**，引用时须区分 |
| MemoryOS | Memory OS of AI Agent | 2025 | arXiv preprint（待核实） | https://arxiv.org/abs/2506.06326 | Lead 表标待核实。★ 本轮经 ACL Anthology 抓 `2025.emnlp-main.1318.bib`，booktitle=「Proceedings of the 2025 Conference on Empirical Methods in Natural Language Processing」，pp.25961–25970 → **实为 EMNLP 2025 main**，建议 Lead 复核后更新 |
| Memory-R1 | Memory-R1: Enhancing Large Language Model Agents to Manage and Utilize Memories via Reinforcement Learning | 2025 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2508.19828 | paper_get（OpenAlex/Crossref 仅给 arXiv DOI） |
| REMem | REMem: Reasoning with Episodic Memory in Language Agent | 2026 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2602.13530 | paper_get（OpenAlex；DOI 10.48550/arxiv.2602.13530） |
| CoALA | Cognitive Architectures for Language Agents | 2023/2024 | TMLR | https://arxiv.org/abs/2309.02427 | Lead 表（arXiv Comments + OpenReview） |
| Lost in the Middle | Lost in the Middle: How Language Models Use Long Contexts | 2024 | ★ Transactions of the Association for Computational Linguistics | https://doi.org/10.1162/tacl_a_00638 | ★ 表内未收录；paper_get 返回 venue=「Transactions of the Association for Computational Linguistics」、type=article、DOI 10.1162/tacl_a_00638 |
| Horizon Gap | The Horizon Gap: Planning, Memory, Execution, Training, and Evaluation for Long-Horizon LLM Agents | 2026 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2608.06663 | paper_get（OpenAlex；DOI 10.48550/arxiv.2608.06663） |
| Ground Truth First | Ground Truth First: A Longitudinal Evaluation Instrument for Agent Memory, and the Tenure Crossover in Memory-Architecture Rankings | 2026 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2607.21962 | paper_get（OpenAlex；DOI 10.48550/arxiv.2607.21962） |
| Storage to Experience | From Storage to Experience: A Survey on the Evolution of LLM Agent Memory Mechanisms | 2026 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2605.06716 | paper_get；arXiv Comments 自填「Accepted by ACL 2026 Findings」，**未独立核 ACL Anthology，标待核实** |
| Learning to Remember | Learning to Remember: Advances in Memory Mechanisms for Large Language Models | 2026 | arXiv preprint（会议归属待核实） | https://arxiv.org/abs/2610.35089 | paper_get（OpenAlex；DOI 10.48550/arxiv.2610.35089） |

### 待核实清单
1. **HippoRAG 的 PPR 实现文件**：论文明确声明用 Personalized PageRank；我实读到 `HippoRAG.py` 的 `index/delete/retrieve/rerank_facts` 与 `embedding_store.py`，但 `src/hipporag/` 下探测的多条 PPR 路径均 404，具体实现文件未定位。
2. **Letta 的 block ORM 字段全集**：`letta/orm/block.py` 与 `letta/schemas/block.py` 在 archive 分支抓取超时（http 000）。已确证的是 `block_manager.py:32` 的字段集与 `:813` 的 limit 强制。
3. **A-MEM / Voyager / Generative Agents / GraphRAG 的官方 repo 文件级核对**：本轮 GitHub API 限流 + github.com 不可达，仅核到 repo 存在，未逐文件读实现。
4. **MemoryOS 的 EMNLP 2025 归属**：ACL Anthology bibtex 证据明确，但按规则以 Lead 表为准，故正文仍写「arXiv preprint（待核实）」，请 Lead 定夺。
5. **MemOS 两条 arXiv 记录的版本关系**（2505.22101 vs 2507.03724）：标题不同，是否为同一工作的改名重发未确认。
