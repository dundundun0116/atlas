# Atlas 项目概述

> 状态：v0.3（2026-10-03；决策 #11–#17 于 2026-10-10 商定）——M0 完成；M2 图状态记忆 + 结转式上下文管理已实现（含评测接入层）；M3.5 多智能体编排已实现；M1 抽样跑分暂停（旧口径那篇已存档），**小批量真实跑分验证通过**（链路 / 提取器入图 / 无空答案）；M4 交付物部分就绪（README / 架构图 / 离线 demo / 快照导出 / 汇总工具）。TDD 86 测试全绿。待 M3 主评测跑分与成绩表。进度明细见第 11 节。

## 1. 一句话定位

**Atlas 是一个面向 long-horizon 任务的多智能体系统：以带时效的图状态记忆承载执行状态（记忆增强），以结转式上下文管理衔接状态层与模型上下文，多智能体围绕共享状态图协作编排，并在现有 long-horizon 评测上量化验证。**

英文题目候选：*Atlas: Graph-Memory, Context Management, and Multi-Agent Orchestration for Long-Horizon Tasks*

## 2. 选题四要素与逻辑关系

| 要素 | 内容 |
|---|---|
| 场景 | long-horizon 任务：数百步、跨会话、子任务相互依赖 |
| 记忆增强 | 执行状态（目标/子任务/约束/决定/产出物/环境事实）以图结构承载；节点/边带时效（作废不删除）；决定被推翻记录原因与依据（决策变迁） |
| 上下文管理 | 结转式压缩（历史压缩前先把状态结构化写入记忆引擎）+ 按配比组装上下文 + 稳定前缀命中服务商缓存 |
| 多智能体编排 | 多角色围绕共享状态图协作，协调经由图而非直接对话；角色数量与组合待定 |

逻辑链：long-horizon 导致上下文超限与状态丢失 → 图状态记忆承载执行状态（对症：压缩丢结构、检索断依赖链）→ 上下文管理是状态层与模型上下文之间的通道 → 多智能体共享同一张状态图协作（同一引擎服务两种场景：单智能体治压缩丢状态，多智能体治共享记忆失效——对应 Governed Shared Memory 形式化的失败模式：过期传播 / 矛盾共存 / 来源崩塌）。

## 3. 项目目标与形态

- **目标：实习求职准备**。成果物 = GitHub 开源项目（README、架构图、可运行 demo、量化成绩、复盘博客）。**不是论文**，创新性不作要求；调研文献只为借鉴思路。
- **实现形态（基座/语言）不作为决策因素**（2026-10-03 用户明确）：以"每个项目细节都能讲明白"为唯一标准。
- pi 源码精读成果（执行循环钩子设计、系统消息补丁机制、压缩切点算法、树状会话存档、上下文编辑条目）转为**设计参考与面试谈资**，不再约束实现路线。

## 4. 核心事实依据（已核实，2026-10 调研）

- **MemoryArena 接口**（源码已核实）：agent 侧为可替换实现（抽象基类，两个必实现方法），评测只观察外显行为 → 多智能体以**内部编排**形态接入完全合法，数字与官方口径可比。硬边界：每步所有环境交互收敛为单动作（并行操作环境需改 runner，数字与官方口径脱钩）。
- **文献热度**：记忆与上下文管理是当前两大最热组件（A-MEM NeurIPS'25 1117 引；上下文工程综述 160 引）；多智能体×共享记忆交叉点已有形式化失败模式且与图状态特性一一对应；"记忆增强 × 多智能体 × long-horizon"的精确交叉无现成基准直接测量（空位）。
- **编排效果评估有成熟路数**：端到端 A/B（编排开关）、过程质量指标（MARBLE 的里程碑达成率 + 交互质量）、失败分类学（ToolFailBench / TRAJECT-Bench 思路，与本项目违规类型学同族）。

## 5. 工作点（范围级，细节待商讨）

- **P1 图状态记忆引擎**：schema（六类节点 + 时效字段 + 决策变迁 + 行动者字段预留多写者）；作废机制 = 时效字段+决策变迁（status 生命周期 + superseded_by 边，Decision 带 reason/evidence，不建执行树）；写入/读取接口（读只取 active）；存储形态。
- **P2 上下文管理**：结转式压缩（替换总结式压缩；触发 = 子目标边界优先 + 80% 阈值强制兜底）；上下文组装器（四区：计划/约束、图摘要 C、近期原始 R、提示 H；各区预算动态适配，固定配比作消融对照；前缀追加为主）；规则式结构查账（活跃 Constraint/Decision 节点缺席即报错）。
- **P3 多智能体编排**：首发角色 Planner / Worker / Auditor（内部编排形态，协调全经共享状态图；Curator 留作规模曲线第 4 点）；LangGraph 承担角色编排控制流，状态唯一真源为自研图引擎（纪律见第 9 节 #9）。
- **P4 评测**：MemoryArena 接入（含 long_context / 总结式对照组）+ 违规类型学（遗忘型 / 记错型 / 使用已作废信息型）+ 编排效果评估（首发：编排开关 A/B + 角色消融 + 成本核算；auditor 过程指标、规模曲线时间允许再做）；协议 = 官方 runner + 抽样子集（每域固定 n、固定种子、多次重复）；对照阵容 = 复现 MAGE 简化版（C+R 组装），时间不允许降为引用其论文数字。
- **P5 开源交付**：README（架构图 + 成绩）、可运行 demo（CLI + 图状态 HTML/SVG 快照导出 + streamlit/gradio 演示页）、复盘博客；权限 = 工具白名单 + 只读/可写分级。

## 6. 背景与问题

- 长任务必然超出上下文窗口，历史必须压缩；压缩丢状态 → agent 违反早期约束、重复犯已修正的错误、目标漂移。
- 现有两类方案的缺陷：**总结式压缩**丢结构与精确值；**语义检索式记忆**（Mem0 / GraphRAG / HippoRAG 一类）在相互依赖任务上打碎决策链（state fragmentation），MemoryArena 实测部分系统还不如完整保留历史。
- 佐证数据：MemoryArena（701 个人工构造的多会话相互依赖任务）上，现有记忆系统任务全对率最高约 29%，2D 结构化记忆最好成绩约 17%（GraphRAG）、12%（Mem0-g）；所有方法随子任务深度全线衰减。
- 结论：执行状态需要一个不依赖语义相似度、也不依赖自由文本摘要的结构化承载——图状态记忆；多智能体场景下，同一结构同时解决共享记忆的过期传播、矛盾共存、来源崩塌。

## 7. 参考与依据

> 三大工作点的详细调研（MAGE 精读、三条线做法分类、可借鉴清单、论文索引）见 [SURVEY.md](SURVEY.md)。

**方法依据（论文/基准）**
- MemoryArena（arXiv 2602.16313）：主基准，多会话相互依赖任务，agent 侧可替换（源码已核实）
- MAGE（arXiv 2606.06090）：同题最近邻（记忆即执行状态管理，未开源），方法与消融的主要参照——全文已精读（notes/MAGE-精读.md）
- TokenMizer（arXiv 2606.06337）：图 schema 的裁剪起点
- AgentFold（arXiv 2510.24699）：上下文折叠线，范式对照
- A-MEM（arXiv 2502.12110，NeurIPS'25）：记忆自组织，记忆线代表作
- Governed Shared Memory（arXiv 2606.24535）：多智能体共享记忆失败模式的形式化——多智能体工作点的动机文献
- MARBLE / MultiAgentBench（arXiv 2503.01935，ACL'25）：编排效果评估路数（里程碑 KPI + 交互质量 + 拓扑对比）
- TokenPilot（arXiv 2606.17016）：前缀缓存保持式上下文管理——稳定前缀设计的同向佐证
- Plans Don't Persist（arXiv 2606.22953）：上下文管理丢失计划导致任务失败——记忆增强工作点的动机文献
- MIRIX（arXiv 2507.07957）：多智能体记忆系统先例（Curator 角色的文献支撑）
- WideSeek-R1（arXiv 2602.04634）：宽度（agent 数）扩展曲线思路
- ToolFailBench（arXiv 2607.04686）/ TRAJECT-Bench（arXiv 2510.04550）：失败分类学与轨迹级评测方法论

**工程参考**
- pi（earendil-works/pi）：源码已精读（执行循环 / 模型接入 / 会话树 / 压缩 / 扩展系统），设计借鉴来源
- MemoryArena 仓库（ZexueHe/MemoryArena）：评测协议与基线客户端，源码已核实
- Anthropic《Building Effective Agents》《Effective Context Engineering for AI Agents》、Manus 博客、12-factor agents

## 8. 里程碑（方向级）

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| M0 | 精读：pi 源码 + MemoryArena 客户端 + MAGE 全文 | 挂接点清单、schema 裁剪输入 | ✅ 完成（2026-10-03，MAGE 笔记见 notes/） |
| M1 | 评测环境跑通 + 对照组数字对齐 | 环境 ready | 🟡 全量跑分暂停（旧口径 1/10 篇已存档）；**小批量验证通过**（2 子任务 1/2 正确、答案非空、提取器入图正常）；全量待端点快时段 |
| M2 | 图状态记忆引擎 MVP + 结转式上下文管理 | 在 formal_reasoning 域胜过 long_context 对照 | 🟡 接入层已实现 + 小批量真实跑分验证通过（结转 applied=2、图快照正确）；待双臂全量对照 |
| M3 | 主评测跑分 + 类型学统计 | 成绩表、SR@k 曲线、类型学统计 | 🟡 **三臂初步数字已出**（2 篇 9 子任务/臂：atlas 0.643 = mage 0.643 > 基线 0.571，上下文 1079/397/711）；orch 缓跑；补样本与全量待方向确认 |
| M3.5 | 多智能体编排接入 + 编排效果评估 | 编排对照结果 | 🟡 编排实现完成（决策 #15–#17，角色图接口端到端验证通过）；编排臂真实跑分待验证 |
| M4 | 开源交付 | README、demo、复盘博客 | 🟡 README 正式版 + 架构图（D2/SVG）+ 离线 demo + streamlit 演示页 + 图快照导出 + 结果汇总工具 + 复盘博客草稿（成绩占位）已就绪；成绩表待跑分回填 |

## 9. 决策清单（v0.3，2026-10-03 逐项商定；#11–#17 于 2026-10-10 商定）

| # | 事项 | 决策 |
|---|---|---|
| 1 | 图作废机制 | 时效字段+决策变迁：节点 status 生命周期（active→superseded/expired）+ superseded_by 边 + 时间戳；Decision 节点带 reason/evidence；不建执行树 |
| 2 | 结转触发 | 子目标边界优先（依赖链中间绝不切）+ 上下文用量 80% 阈值强制兜底 |
| 3 | 查账机制 | 规则式结构查账：状态区一律从图重新生成，活跃 Constraint/Decision 节点缺席即报错；零额外 LLM 成本 |
| 4 | 组装配比 | 四区结构（计划/约束、图摘要 C、近期原始 R、提示 H）+ 各区预算动态适配（按图状态规模/任务阶段）；固定配比保留作消融对照 |
| 5 | 角色组合 | 首发 Planner/Worker/Auditor，协调全经共享图；Curator 留作规模曲线第 4 点 |
| 6 | 编排评估指标 | 首发：编排开关 A/B + 角色消融 + 成本核算；auditor 过程指标与规模曲线时间允许再做 |
| 7 | 评测协议 | 官方 runner + 抽样子集（每域固定 n、固定种子、多次重复），报成绩注明抽样口径（2026-10-10 深夜修订：全量改 **2 篇 9 子任务/臂** 的小清单初步数字，方向明确可补测第 3 篇；多次重复视情况） |
| 8 | 对照阵容 | 复现 MAGE 简化版（C+R 组装、去树回滚）作中间对照；时间不允许降级为引用 MAGE 论文数字 |
| 9 | 实现底座 | LangGraph 仅作角色编排控制流（数据流不走框架 state）；状态唯一真源 = 自研图引擎，不启用框架 checkpoint；若实现中该纪律不可守，退回纯 Python 自写（用户预设的回退条件） |
| 10 | harness 周边 | CLI 评测 + CLI demo + 图状态 HTML/SVG 快照导出 + streamlit/gradio 演示页；权限 = 工具白名单 + 只读/可写分级 |
| 11 | 结转节奏（math 域落地） | 每子任务边界结转：wrap 到新子任务时先结转上一个（LLM 提取入图 + 叙事摘要）；80% 阈值保留为兜底 |
| 12 | 提取器分工 | 规则+LLM 混合：规则负责子任务注册/完成标记/判分记录；LLM 只抽语义状态（Fact/Constraint/Decision/Artifact，含 supersede 作废更新），输出严格 JSON，解析失败降级为规则摘要 |
| 13 | Atlas 上下文预算 | 8000 tokens（组装器总预算；实测本域自然需求 3–5k，基线全篇 10–25k） |
| 14 | judge 口径 | 两臂 `judge_result_in_memory=true`（判分进记忆条目，公平可比）；旧口径（false）已跑那篇存档 `runs/stale_judge_false/`，待重跑 |
| 15 | 编排形态（M3.5） | 固定三段式：Planner 定路线（Decision）→ Worker 解题（Artifact 草稿）→ Auditor 核验（Fact 疑虑）→ 仅当 Auditor 判"需修订"时 Worker 修订一次（修订稿作废草稿、记原因） |
| 16 | 角色协调通道 | 协调全经共享图：角色结论/疑虑经 `/memory/role_note` 落成图节点（actor 标角色），下一角色经 `/memory/state_view` 读图状态视图而非对话历史 |
| 17 | 编排开关 | config 一行（`agent.orchestration`）：入口脚本据此替换 runner 的 MathAgent（不改官方代码）；编排臂 `memory_system_name=atlas_orch` 使结果落独立目录 |

已定不再重议：节点六类（Goal/Subtask/Constraint/Decision/Artifact/Fact）+ 行动者字段（选题定稿已含）。

## 10. 决策记录

- 2026-10-01：方向定为"图结构增强长执行 agent"；确认 MemoryArena 开源可插拔。
- 2026-10-03（上午）：形态原定 fork pi + 逐部件改造 + 自有品牌（对标 OMP）——同日晚间被下述决策取代。
- 2026-10-03（定稿）：项目目标 = 实习准备（开源 GitHub 即可，非论文，创新性不作要求）；基座与语言不作为决策因素；选题定稿为"**面向 long-horizon 的记忆增强 + 上下文管理 + 多智能体编排**"；多智能体以内部编排形态跑现有 long-horizon 评测（MemoryArena 接口已核实可行）；agent 角色数量与组合待定；术语沿用约定——统一用"图状态记忆/状态层"，禁用"账本"比喻。
- 2026-10-03（晚）：第 9 节清单逐项商定并回填（作废机制=时效字段+决策变迁；结转=子目标边界+阈值兜底；查账=规则式；组装=四区+动态预算；角色=Planner/Worker/Auditor；指标=三件套；协议=官方 runner+抽样；对照=复现 MAGE 简化版；harness=CLI+快照+Web UI）。底座定为 LangGraph：仅承担角色编排控制流，角色间数据流走共享图而非框架 state，不启用框架 checkpoint；实现中若此纪律不可守，按用户预设条件退回纯 Python 自写。
- 2026-10-10：M2 接入层四项决策商定（#11 每子任务边界结转；#12 规则+LLM 混合提取器；#13 预算 8k；#14 两臂 judge 入记忆），同日落地实现（LLM 结转提取器 + AtlasMemorySystem + 分派式 memory server；TDD 59 测试）。
- 2026-10-10（深夜）：评测口径修订 + MAGE 臂落地。用户拍板：全量 95 子任务/臂不跑，改 **2 篇小清单（索引 39/36，9 子任务/臂）初步数字，每臂约 1 小时量级**，方向明确可补测第 3 篇（索引 22）；MAGE 简化基线**实现并跑同一清单**（决策 #8 执行，不做降级）；角色消融留作条件项（初步数字方向明确且时间允许时做，首选"去 Auditor"）。跑分链已按新清单重启（基线 → atlas → orch，2 篇清单）。
- 2026-10-10（晚）：M3.5 三项决策商定（#15 固定三段式编排；#16 协调全经共享图；#17 config 一行编排开关）。附两条实现级记录：① `SUPERSEDES` 边扩展到 Artifact→Artifact（编排修订必须能作废错误草稿，否则草稿会作为活跃产出物留在图里，与"防矛盾共存"直接冲突）；② 推理开关——端点实测支持 `thinking.type=disabled`/`reasoning_effort=none`（推理 token 归零、快一个数量级），机械性调用（提取器、Planner/Auditor 的 JSON 输出）一律关推理，解题调用（Worker/修订）保持推理、空正文时关推理重试一次。

## 11. 进度日志（最新在上）

- 2026-10-11（凌晨）：**三臂初步数字出齐（缩减口径：2 篇 9 子任务/臂）**——官方口径成绩：atlas **PS 0.643** / mage（叙事摘要基线）**0.643** / 基线 **0.571**；pass rate 三臂均 0.500；平均上下文 tokens atlas 1079 / mage 397 / 基线 711（pooling 子任务正确率 4/9 / 4/9 / 3/9，基线含 1 个空答案——推理失控属端点行为，各臂平等暴露）。**解读**：图状态记忆 ≥ 基线成立，但与叙事摘要基线打平——"结构化 vs 摘要"核心假设在 n=2 上无区分度；省 token 卖点未显形（math 域每步记录小，基线全历史也不长）。orch 臂按"设计依赖"缓跑（链在 atlas 臂后自动掐断，零浪费）；mage 臂接力跑完（同清单）。成绩表已回填 README 与博客。**下一步选项（待拍板）**：a) 补测第 3 篇（索引 22，7 子任务，三臂 ~1-2h）拉样本；b) 跑 orch 回答编排增量；c) 先转交付物。冒烟记录：mage 臂冒烟链路全通（摘要质量在线），2308.12401 上 0/2 空答案系模型推理失控（49 分钟/子任务），非实现问题。
- 2026-10-10（深夜·二）：**MAGE 简化基线臂落地（决策 #8 执行）+ 跑分链切 2 篇清单**——`atlas/mage_baseline.py`（MAGELiteMemorySystem：子任务边界 LLM 叙事摘要累计、R 恒空，无图无预算；失败降级为截断原文；carry_log/extract_errors 与 Atlas 同名，服务端日志零改动复用；TDD 8 测试）+ server 分派 `mage` + `eval/configs/math_mage.json` + `ATLAS_SERVER_PORT` 环境变量（跑分进行中改代码时另起 8002 实例，不动主服务）。冒烟进行中（2308.12401×2 子任务）。正式链已切 2 篇清单（39/36，9 子任务/臂）三臂后台跑。TDD **95 测试全绿**。
- 2026-10-10（深夜）：**A/B 路线启动（用户拍板做 A+B）**——编排臂冒烟通过（`2507.18621`，2/2 正确，编排臂首次真实跑分验证）；三臂**3 篇小论文初步数字跑分后台进行中**（索引 39/36/22 = 2+7+7 子任务，`SUBSET_INDICES` 机制新增于 run_math_subset.py，顺序 39→36→22，基线臂先跑；预计数小时，断点续跑）；A 路线交付 streamlit 演示页（`atlas/demo_app.py`，AppTest 回归测试，87 测试全绿）+ 复盘博客草稿（`docs/blog-draft.md`，成绩占位）。**待办**：跑分完成后 `summarize.py` 出初步成绩表 → 回填 README 与博客。
- 2026-10-10（夜）：**小批量验证通过 + 状态快照**——`SMOKE_SUBTASKS=2`（Atlas 臂，agent max_tokens 32000）：2 子任务 **1/2 正确**（st0 错 884s / st1 对 615s），答案均非空（693 / 1572 字符，空答案问题已解决）；结转 `applied=2 skipped=2`（护栏拦下 2 个非法操作）、提取器真实入图（`F1` Tate 界 / `A1` 结论，actor=extractor）、图快照落盘正常；汇总表（官方口径）PS 0.500 / pass rate 1.000 / 平均上下文 316 tokens（n=1 篇，仅验证链路，不作数字）。**当前状态**：M0 完成；M2 接入层完成并小批量验证通过；M3.5 编排实现完成（角色图接口端到端验证通过）；M4 交付物部分就绪（README / 架构图 / 离线 demo / 快照导出 / summarize）；TDD 86 测试全绿。**卡点**：端点吞吐波动约 10 倍（今早 164s/子任务 vs 晚间 300–1900s），全量 95 子任务/臂按晚间速度约 19 小时——待端点快时段或另定口径。**下一步（待用户拍板）**：A 继续交付物（streamlit 演示页 + 复盘博客草稿）；B 再挑 2–3 篇小论文攒初步数字；C 等快时段跑全量三臂。
- 2026-10-10（晚·二）：**M4 交付物起步 + 评测工具**——README 正式版（定位 / 问题 / 三个工作点 / 评测协议 / 快速开始 / 仓库结构 / 状态；成绩表留占位待 M3）+ 架构图（`docs/architecture.d2` → SVG，D2 elk 布局：runner → 记忆系统 → 编排）+ 离线 demo（`atlas/demo.py`：脚本化提取器跑旅行规划迷你会话，打印四区组装、导出图快照，无需 API 凭据）+ `eval/summarize.py`（复用官方 eval 口径，汇总各臂 PS / pass rate / memory_length 为 Markdown 表并写官方 all_results.json）。评测配置：三个 config 的 agent `max_tokens` 8192 → 32000（推理型模型空答案问题）；冒烟脚本支持 `SMOKE_SUBTASKS=<K>`（只跑前 K 个子任务）且冒烟不做断点续跑。TDD 86 测试全绿。
- 2026-10-10（晚）：**M3.5 多智能体编排实现（决策 #15–#17）**——`atlas/orchestration.py`（固定三段式编排器：Planner 定路线写 Decision → Worker 解题写 Artifact 草稿 → Auditor 核验写 Fact 疑虑 → 需修订时 Worker 修订并作废草稿；LLM 与图客户端注入可测）+ `eval/atlas_agent.py`（OrchestratedMathAgent 挂官方 MathAgent，入口脚本按 config 一行替换；协调经 memory server 的 `/memory/state_view` 与 `/memory/role_note`；失败逐级降级：编排 → 单发 → 官方 act）+ 服务端角色图接口 + `eval/configs/math_atlas_orch.json`。**关键发现（顺带修复提取器）**：端点支持关推理（`thinking.type=disabled` / `reasoning_effort=none`，实测推理 token 归零、快一个数量级）→ 提取器关推理后从"17 分钟且空正文"变为 8.7s 正常抽取（真实长记录段实测）；机械性角色调用（Planner/Auditor）同关推理，解题（Worker/修订）保持推理且空正文时关推理重试一次。schema 补一类边：`SUPERSEDES` 允许 Artifact→Artifact。TDD 85 测试全绿；角色图接口端到端验证通过（D1/A1/F1/A2 + `A2 -supersedes-> A1`、actor 标记正确、state_view 含作废提示）。
- 2026-10-10（下午·二）：**冒烟确认 + 提取器修复 + 图快照/CLI 起步（决策 #10）**——冒烟抓到三个真问题并修复：① runner 在首个 wrap 前会先 add "Initial result: Empty"，被当成待结转记录（白烧调用）→ 无子任务时直接丢弃；② 提取器空正文（推理 token 吃光 max_tokens）→ 提到 8000 + 空正文诊断日志；③ 记录段里的 `## Tool Calls Info` 是整段工具轨迹（上万字符）→ 按条 3500/全段 7000 字符截断保护。**提取器实测选型**：deepseek-v4.1-flash 在长记录段上推理失控（8000 token 全被推理吃光、单次 17 分钟、常以连接错误告终）、deepseek-flash 63s/推理 7111 token、mimo-v2.6-flash 33s/推理 398 token 且 JSON 合规 → 提取器默认改 mimo-v2.6-flash（ATLAS_EXTRACTOR_MODEL 可覆盖；agent/env 侧仍用 deepseek-v4.1-flash）。迷你会话 + 真实跑分冒烟确认：服务端结转通（`结转#1（S1）：applied=2 skipped=0`，无错误）。新增 `atlas/export.py`（自包含 SVG/HTML 渲染：类型分列、状态配色、边型线型；零依赖确定性输出）+ `atlas/cli.py`（`python -m atlas.cli export`）+ 图快照落盘（服务端 `ATLAS_GRAPH_DUMP_DIR` 默认 runs/graphs/）；8002 临时服务端到端验证（dump → 导出 → SVG 合法 XML）。TDD 72 测试全绿。
- 2026-10-10（下午）：**M2 接入层实现（决策 #11–#14 落地）**——新增 `atlas/context/extractor.py`（LLM 结转提取器：严格 JSON、逐操作校验跳过、DEPENDS_ON 防环、supersede 作废、解析失败降级）、`atlas/service.py`（AtlasMemorySystem：每子任务边界结转、规则注册子任务/判分记录、H 区答错提示、组装失败降级为计划区）、`eval/atlas_memory_server.py`（单端口按 memory_system_name 分派 long_context/atlas，A/B 只差 config 一行；`lite_memory_server.py` 并入后删除）、`eval/configs/math_atlas.json`；双臂 judge 口径统一为 true（决策 #14），旧口径已跑那篇移至 `runs/stale_judge_false/` 存档待重跑；`run_math.sh` 支持传入 config。TDD 新增 17 测试（59 全绿）。跑分仍暂停（按用户指示，等续跑指令）。

- 2026-10-10：**M1 跑分中断诊断 + 代理修复入库**——抽样跑分在 1/10 篇（2312.05134_part_2：8 子任务，2/8 正确，平均 164s/子任务）后中断，根因：shell 继承的 `HTTP(S)_PROXY` 指向 Clash，而 config 本地服务地址写 `0.0.0.0`（不在 `NO_PROXY`）→ 对本地服务的请求误走代理，Clash 短暂不可用即全挂；进程随后消失（疑随会话回收）。修复：`run_math.sh`/`start_servers.sh` 补 `no_proxy` 旁路（0.0.0.0/127.0.0.1/localhost）、config 本地 base_url 改 `127.0.0.1`、README 补代理与 nohup 说明；正反两组验证通过（死代理下本地调用仍通）。**用户拍板：暂不续跑**（剩余 9 篇/87 子任务，断点续跑就绪）；M2 收尾按"先摊开设计再动手"推进（提取器与接入形态给选项待拍板）。附带记录上游两处怪癖：runner 尾部 `main()` 被调用两次（第二遍因断点续跑全跳过，无害）；`is_paper_correct` 取的是末题正确性。
- 2026-10-10：**M1 抽样跑分启动（决策 #7）**——opencode go 通道接入完成：端点 `opencode.ai/zen/go/v1` 必须带 `x-opencode-session` 与自有 User-Agent 头（缺头 400），补 `eval/opencode_compat/sitecustomize.py` 自动注入，实测简单调用 ~2s（对比 NIM 的 98s）。**修正**：初次误按全量 40 篇启动（偏离决策 #7 的抽样协议，已停），改为固定子集 **n=10、seed=20261010**（清单落 `runs/subset_formal_reasoning_math_n10_seed20261010.txt`，共 95 个子任务；Atlas 对照跑用同一清单）。踩坑与修复：① HF hub 偶发失败会触发 datasets 库缓存回退 bug（split 被拼进 config 名→ValueError），wrapper 改为**本地缓存直载优先**（`run_math_subset.py`），完全绕开 hub 依赖；② 后台输出要 `python -u` 才可见。抽样跑分后台执行中（断点续跑）。
- 2026-10-03（夜）：**M2 离线核心完成**——`atlas/memory/`（图引擎：六类节点/五类边/status 生命周期/决策变迁/JSONL 持久化）+ `atlas/context/`（四区组装器+动态预算+规则查账；结转触发+结转管理），TDD 42 测试全绿（24d4581）。测试抓到两个真问题：决策链回溯方向反了；Fact/Artifact 在组装结果无出口→计划区补"环境事实/产出物"两节。工作约定确立（做完即提交+更新文档，见 AGENTS.md）。
- 2026-10-03（夜）：**M1 环境就绪**——MemoryArena 克隆至 external/；uv venv（Py3.12）+ math 域必需依赖；lite memory server（eval/lite_memory_server.py，复用官方 LongContextMemorySystem 类，端口 8000）；math 域数据集验证（40 任务，与 MAGE 口径一致）；long_context 配置模板（39beb97）。模型后端暂定 NVIDIA NIM deepseek-v4.1-flash（免费档，key 在 eval/.env），实测该端点延迟 ~98s/请求不可用，待用户切换 opencode go 订阅后跑 smoke + long_context 对齐。
- 2026-10-03（晚）：十项决策商定回填（第 9 节，v0.3）；MAGE 全文精读完成（notes/MAGE-精读.md，M0 收尾）；GitHub 私有仓库建立（dundundun0116/atlas）并推送全部文档；提交邮箱换为 GitHub noreply。
