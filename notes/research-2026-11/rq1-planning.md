# RQ1 长程规划与任务分解

> 范围：ReAct / Plan-and-Solve / ToT / LATS / Reflexion / SayCan / Voyager / SWE-agent / OpenHands / AutoGen / LangGraph + 6 个近三年规划类工作（LLMCompiler、ADaPT、DEPS、ReWOO、HuggingGPT、Magentic-One），共 17 个。
> 元数据：venue 以 `notes/research-2026-11/venue-verified.md`（Lead 核验）为准，本轮独立复核项见 §6。
> 工程证据：全部经 `raw.githubusercontent.com` 实读源码或 `git ls-tree` 实读文件树，路径与符号可直接在原仓库定位。

---

## 0. 结论摘要（硬结论）

1. **17 个工作里，运行时真正持有"计划数据结构"的只有 5 个**：LATS 的 MCTS `Node` 树（`hotpot/lats.py:87`）、LLMCompiler 的 `Task.dependencies` 整数 DAG（`task_fetching_unit.py:42`）、HuggingGPT 的 `command["dep"]` 数组（`awesome_chat.py:261`）、ToT 的 beam 列表（`bfs.py` 的 `ys`）、Magentic-One 的 Task/Progress 文本账本。其余 12 个的"计划"要么是提示里一段文字，要么不存在。

2. **论文里的"树"不等于实现里的"树"**。ToT 开源实现只有 `src/tot/methods/bfs.py`（该目录**只有 bfs.py**，无 dfs.py），且 `solve()` 的 `ys` 只是候选字符串列表，**无 parent 指针、无回溯、无节点状态**，历史只落在 `infos` 日志。"DFS 可剪枝"仅是论文主张。

3. **"子目标级局部重规划"在这 17 个工作里出现 0 次**。可观测的重规划粒度只有两种：整段重来（Reflexion 外层 `while trial_idx < num_trials`、SWE-agent `ScoreRetryLoop`）与整计划重写（LLMCompiler `max_replans`、Magentic-One `_update_task_ledger`）。**这是 Atlas 的真空位。**

4. **step 级持久化只有 LangGraph 做了，且它持久化的是"通道版本"而非"计划"**。`_loop.py::tick()` 每超步落 checkpoint，`_algo.py::apply_writes()` 的 `channel_versions` / `versions_seen` 才是恢复依据；计划节点没有"已完成/失败"字段。

5. **LangGraph 不是 DAG 执行器，是 BSP 超步引擎**。`_algo.py::_triggers()` 的触发条件是"节点所读 channel 版本 > 上次所见版本"，不是"前驱全部完成"；拓扑由 `StateGraph.add_edge()` 静态声明（`graph/state.py:928`），支持环。当"DAG 编排"宣传是常见误解。

6. **失败处理全谱系只有 4 种原语**：重试同一步（SWE-agent、Voyager `action_agent_task_max_retries=4`）、跳过（Voyager `curriculum.py:337` "Skipping to next task"）、整段重跑（Reflexion / reviewer）、文本反思（Reflexion、LATS `reflection_map`）。**缺第五种：改依赖图**。

7. **计划可执行性在工程实现里几乎不判定**：只有 LangGraph 在 `compile()` 期做静态校验，HuggingGPT 用 `if dep_id >= task["id"]: task["dep"] = [-1]` 把前向/循环依赖**静默丢弃**；LLMCompiler 的 `_get_all_executable_tasks()` 不做环检测，环会让 `schedule()` 永久空转。

8. **Atlas 的落点是"把计划变成一等状态"**。已有 `TaskState` 状态机（`atlas/orchestration/state_machine.py:23` 的 PLANNING/EXECUTING/AUDITING/REPAIRING/PAUSED/COMPLETED/FAILED）、checkpoint、event log、replay、幂等，但停在 **task 粒度**；`models.py` 只有 Session/Node/Edge/Event/Checkpoint/RunMetric 六张表，**无 Step/Dependency/PendingToolCall**；`Orchestrator.solve(..., subtask_id="S1")` 把"单子任务"写进了签名。

---

## 1. 机制谱系与选型

### 1.1 五档谱系

| 档位 | 计划载体 | 代表 | 计划可修改性 | 可恢复性 |
|---|---|---|---|---|
| L0 内隐 | 无（提示里的 Thought 串） | ReAct、SWE-agent、OpenHands | 不可修改 | 仅轨迹日志 |
| L1 线性 | 有序子目标列表 | Voyager `sub_goals`、Plan-and-Solve | 可跳过不可重排 | 弱（进度=完成计数） |
| L2 依赖图 | DAG | LLMCompiler、HuggingGPT、ADaPT | 可整图重写，不可局部改边 | 中（多在全内存） |
| L3 树搜索 | 搜索树/beam | ToT（beam）、LATS（MCTS） | 可回溯剪枝 | 无（LATS 全内存） |
| L4 分层账本 | 目标→子目标→动作 + 进度账本 | Magentic-One、SayCan | 整账本重写 | 中（无 step 持久化） |

### 1.2 结构选型决策表

判据：**可分解性**（能否拆成可独立验证的子目标）、**依赖密度**、**可回滚性**、**单步成本**、**观测确定性**。

| 任务特征 | 推荐结构 | 理由 | 反例警告 |
|---|---|---|---|
| 可分解性低 + 依赖密度高 + 单步便宜 | **线性** | 拆不开别硬拆，最省 token 最易 replay | 强上 DAG 会引入无法验证的伪依赖 |
| 可分解性高 + 依赖中等 + 观测确定 | **DAG**（静态计划+动态调度） | 依赖可并行可缓存的唯一场景 | 生成后不改=过早固化（只能靠 `max_replans` 补救） |
| 可分解性高 + 可回滚 + 单步昂贵 | **树搜索** | 高成本单步必须"想清楚再动手" | 环境不可回滚时树搜索付出真实副作用 |
| 可分解性低 + 观测不确定 + 需多轮试错 | **反思循环** | 无可分解结构，只能整段重来+语言反思 | 长任务上成本线性爆炸 |
| 子任务异构（检索/代码/编辑）+ 依赖稀疏 | **分层** | 每层用不同验证器，层间契约稳定 | 层数>2 时上层账本退化成不可校验的长文本 |
| 长程（>30 步） | **分层 + step 级持久化**（Atlas 目标） | 只有 step 级状态能崩溃后精确续跑 | 照搬 LangGraph 会把计划埋进数据流，无法诊断计划本身 |

### 1.3 何时用哪种（可操作判据）

- **这一步失败了，能不能只重做这一步？** 能 → 至少需 step 级状态（L2+）；不能（副作用不可逆）→ 需 L3 或人工确认点。
- **子任务之间有没有数据依赖且多于一条？** 有 → DAG；纯顺序 → 线性，别过度设计。
- **计划写错了我怎么知道？** 每步有可执行 success predicate → 可自动重规划；没有 → 只能 LLM 自评（可靠性低，需 Auditor 兜底）。

---

## 2. 逐工作分析

### ReAct
- 类型：论文（附 notebooks 参考实现）｜年份：2022（arXiv）/ 2023（ICLR）
- 会议或来源：ICLR 2023｜官方链接：https://arxiv.org/abs/2210.03629 ；代码 https://github.com/ysymyth/ReAct
- 主要解决的问题：让推理与行动在同一条轨迹交替，用观测纠正幻觉。
- 核心机制：Thought → Action → Observation 循环，观测回填 prompt 再生成下一 Thought。
- 关键数据结构：**没有**；唯一状态是不断增长的 prompt 字符串。
- 执行流程：`webthink()` 里 `for i in range(1, 8)` 固定上界；`llm(prompt + f"Thought {i}:")` 用 stop token 卡住 `Observation {i}:`，解析 action 后 `step(env, action)` 取观测。
- 如何处理失败：解析失败时 `try/except` 退化为"取首行当 thought，再单问一次 action"（`n_badcalls` 计数）；步数耗尽强制 `finish[]`。
- 如何处理状态变化：无显式状态，环境状态只在 prompt 文本与 env 内部。
- 如何处理记忆：整条轨迹即记忆，无压缩无检索。
- 如何评测：HotpotQA / FEVER / ALFWorld / WebShop 成功率。
- 真实证据：`hotpotqa.ipynb::webthink()`（实读，6 cell notebook，非库代码）；`prompts/prompts_naive.json` 提供 few-shot。
- 对 Atlas 的可借鉴点：观测必须回填的最小闭环——把"本步观测"设为 Step 必填字段，而非塞进上下文。
- 不适合 Atlas 的地方：无计划、无状态、无恢复；7 步硬上界与长程冲突。
- 可信度：**高**（论文与 notebook 一致，机制无歧义）。

### Plan-and-Solve
- 类型：论文（提示词方法）｜年份：2023｜会议或来源：ACL 2023
- 官方链接：https://arxiv.org/abs/2305.04091
- 主要解决的问题：zero-shot CoT 的两类失败（计算/常识错误、漏步），用"先计划再执行"两段式提示缓解。
- 核心机制：两段提示——先 "devise a plan"，再按计划 carry out 并强调中间计算；PS+ 变体追加"提变量/算中间结果"。
- 关键数据结构：自然语言计划段落。
- 执行流程：单次 LLM 调用产出计划+执行，无多轮交互（提示技巧，不是 agent）。
- 如何处理失败：无验证、无重试。
- 如何处理状态变化：无环境交互。
- 如何处理记忆：无。
- 如何评测：10 个推理数据集（GSM8K、SVAMP、AQuA 等）zero-shot 准确率。
- 真实证据：**仅论文机制**。`git clone https://github.com/hengjui/Plan-and-Solve` 返回 `repository not found`，**官方仓库未定位**，工程实现标"待核实"。
- 对 Atlas 的可借鉴点：把"制定计划"与"执行计划"在**上下文上物理隔离**（四区组装分出"计划区"），避免执行细节污染计划。
- 不适合 Atlas 的地方：单轮提示技巧，无 step、无依赖、无失败处理，不能作为 Harness 结构。
- 可信度：**中**（机制清楚，无代码可核，无法判断计划是否真被后续步骤遵守）。

### Tree of Thoughts (ToT)
- 类型：论文 + GitHub｜年份：2023｜会议或来源：NeurIPS 2023（oral）
- 官方链接：https://arxiv.org/abs/2305.10601 ；代码 https://github.com/princeton-nlp/tree-of-thought-llm
- 主要解决的问题：CoT 是单向贪心，局部走错无法回退；把推理变成 thought 级搜索。
- 核心机制：四件套——thought 生成（sample/propose）、状态估值（value/vote）、搜索（BFS/DFS）、选择（greedy/sample）。
- 关键数据结构：论文称搜索树；实现是 `ys`（候选字符串列表）+ `infos`（日志列表），**无节点对象**。
- 执行流程：`bfs.py::solve()` → `for step in range(task.steps)` 固定深度 → 扩展 → 估值 → 取 top-`n_select_sample` → `ys = select_new_ys`（即 beam search）。
- 如何处理失败：无显式处理；低估值候选被 beam 淘汰（软失败）。
- 如何处理状态变化：无环境，状态就是部分解文本。
- 如何处理记忆：无跨题记忆，`cache_value` 只缓存估值调用。
- 如何评测：Game of 24、Creative Writing、5×5 Crosswords 成功率。
- 真实证据：`src/tot/methods/bfs.py`（95 行实读）的 beam 更新与 `infos.append(...)`；`git ls-tree` 确认该目录只有 bfs.py，无 dfs.py；入口 `run.py` + `scripts/game24/bfs.sh`。
- 对 Atlas 的可借鉴点：候选生成+显式估值+剪枝三件套只用在**计划生成期**（生成 3 个候选计划，Auditor 打分选 1），成本可控。
- 不适合 Atlas 的地方：beam 无回溯无持久化；固定 `task.steps` 与自适应终止冲突；DFS 分支未开源不能照抄。
- 可信度：**高**（代码与论文 BFS 描述一致；但"树"在实现中是 beam，需纠正通行误解）。

### LATS
- 类型：论文 + GitHub｜年份：2023（arXiv）/ 2024（ICML）｜会议或来源：ICML 2024（PMLR v235）
- 官方链接：https://arxiv.org/abs/2310.04406 ；代码 https://github.com/lapisrocks/LanguageAgentTreeSearch
- 主要解决的问题：把 MCTS 引入语言 agent，统一推理/行动/规划，用环境反馈做价值回传。
- 核心机制：UCT 选择 → 扩展（LLM 生成 k 候选）→ 估值 → rollout → 反向传播；失败轨迹聚合成 self-reflection 注入后续 prompt。
- 关键数据结构：**真树**。`hotpot/lats.py:87` `class Node`：`parent/children/visits/value/is_terminal/reward/exhausted/depth/state`，`uct() = value/visits + sqrt(2*ln(parent.visits)/visits)`。
- 执行流程：`lats_search(iterations=30)` → `select_node(root)`（UCT 下降，全 terminal 子树则 `node.parent.children.remove(node)` 剪枝回溯）→ `expand_node`（`depth>=7` 强制 terminal）→ `rollout(max_depth=4)` → `backpropagate`。
- 如何处理失败：terminal 且 `reward != 1` 的节点被剪掉回父层；`failed_trajectories` → `generate_self_reflection()` → `reflection_map` 注入下一轮（`get_samples` 里 `if len(unique_trajectories) > len(reflection_map)`）。
- 如何处理状态变化：`env.step(action)` 推进；节点 `state` 存 thought/action/observation。
- 如何处理记忆：跨迭代反思文本（`reflection_map`），无持久化。
- 如何评测：HotpotQA、WebShop、HumanEval/MBPP。
- 真实证据：`hotpot/lats.py`（414 行实读）的 `Node.uct`／`select_node` 回溯／`lats_search` 主循环；`programming/dfs.py`（222 行）为编程变体。
- 对 Atlas 的可借鉴点：唯一有"显式父指针+可剪枝"的机制；其"标记失败子树 exhausted 并回退到最近未耗尽祖先"可直接作为 step 级恢复语义。
- 不适合 Atlas 的地方：全内存无 checkpoint 无 event log；30 迭代×k 候选的 token 成本长程不可承受；`depth>=7` 硬上限与长程矛盾。
- 可信度：**高**（算法与数据结构在代码里完整可见）。

### Reflexion
- 类型：论文 + GitHub｜年份：2023｜会议或来源：NeurIPS 2023（poster）
- 官方链接：https://arxiv.org/abs/2303.11366 ；代码 https://github.com/noahshinn/reflexion
- 主要解决的问题：不更新权重，用语言化反思做跨回合策略改进。
- 核心机制：Actor 执行 → Evaluator 给成功信号 → Self-Reflection 生成"下次怎么做"的文字计划 → 存入情景记忆 → 下一回合注入。
- 关键数据结构：每环境一个 dict `{'memory': [], 'is_success': False}`（`alfworld_runs/main.py:48-52`），`memory` 是反思字符串列表。
- 执行流程：`while trial_idx < args.num_trials:` → `run_trial(...)` 跑整局 → `update_memory(trial_log_path, env_configs)` 对未成功且未 skip 的环境生成反思追加进 `memory` → 存 `env_results_trial_N.json`。
- 如何处理失败：唯一手段是"下一整局重来+一句反思"；`if not env['is_success'] and not env['skip']`，且只用最近 3 条反思（`env['memory'][-3:]`）。
- 如何处理状态变化：无 step 级状态，一局结束才更新记忆。
- 如何处理记忆：情景记忆=反思文本列表，滑动窗口 3。
- 如何评测：ALFWorld、HotpotQA、HumanEval（pass@1）。
- 真实证据：`alfworld_runs/main.py`（119 行）与 `alfworld_runs/generate_reflections.py`（47 行）实读；反思 prompt 明写 "Devise a concise, new plan of action that accounts for your mistake"，并拼入 "Plans from past attempts"。
- 对 Atlas 的可借鉴点：**反思的落点是"计划文本"而非过程总结**——Atlas 的 Decision 节点可直接存这类"计划修正条目"，天然带 provenance。
- 不适合 Atlas 的地方：重规划粒度是整局，与局部修复相反；`num_trials` 线性放大成本；反思无校验（缺 Auditor verify）。
- 可信度：**高**（循环与记忆更新逻辑直读可见）。

### SayCan
- 类型：论文｜年份：2022｜会议或来源：CoRL 2022（PMLR v205）
- 官方链接：https://arxiv.org/abs/2204.01691
- 主要解决的问题：LLM 能说不能做——高层步骤在机器人上不可执行；把语言计划接地到可行动作。
- 核心机制：`score = p(LLM 认为该技能有用) × p(affordance 可行)`，逐技能打分取 argmax，执行后再规划下一步（receding horizon）。
- 关键数据结构：无计划图；每步对候选技能集合做一次打分排序。
- 执行流程：高层指令 → LLM 给技能库每项"有用性"分 → value function 给"当前状态可行"分 → 相乘取最大 → 执行 → 重复。
- 如何处理失败：affordance 分数天然过滤不可行技能；执行失败使下一步打分下降。
- 如何处理状态变化：**强项**——每步用当前视觉/状态重算 affordance，等于每步强制重规划。
- 如何处理记忆：无长期记忆。
- 如何评测：101 条真实厨房长程指令的成功率/可执行率。
- 真实证据：**仅论文机制**。官方代码在 `google-research/google-research` 的 `saycan/` 子树，仓库体量大，本轮**未 clone 未实读**，实现标"待核实"。
- 对 Atlas 的可借鉴点："每步用当前状态重算可行性"正是 Atlas 最缺的环境状态感知，应落成 Step 的 `precondition_check`，执行前重算而非沿用计划生成时的判断。
- 不适合 Atlas 的地方：依赖连续值函数（真 affordance 模型），软件/文本环境无此密集信号；每步重打分代价高。
- 可信度：**中**（论文机制清楚；代码未实读，无法确认是否按论文实现）。

### Voyager
- 类型：论文 + GitHub｜年份：2023（arXiv）/ 2024（TMLR）｜会议或来源：TMLR
- 官方链接：https://arxiv.org/abs/2305.16291 ；代码 https://github.com/MineDojo/Voyager
- 主要解决的问题：开放世界终身学习——自动课程 + 可复用技能库 + 迭代纠错，无梯度更新。
- 核心机制：三模块解耦——Curriculum（自动出题）、Action（写代码执行）、Critic（判定完成并给反馈）；技能存为**可执行代码**并向量检索复用。
- 关键数据结构：(a) `sub_goals`：**扁平有序列表**（`decompose_task()` 返回），无依赖无嵌套；(b) 技能库：`skill/skills.json` + Chroma，`retrieval_top_k=5`。
- 执行流程：`inference()` → 无 sub_goals 则 `decompose_task` → `while progress < len(sub_goals)` → 取 `sub_goals[progress]` → 检索 top-5 技能拼 context → `rollout()`（最多 `action_agent_task_max_retries=4` 次）→ `update_exploration_progress(info)`。
- 如何处理失败：`curriculum.py:329` 成功进 `completed_tasks`，失败进 `failed_tasks` 并打印 **"Skipping to next task"**；`clean_up_tasks()` 去重后 dump JSON。**无局部重规划，无依赖回滚**。
- 如何处理状态变化：进度 = `len(completed_tasks)`（`curriculum.py:131`），即用完成数当指针；环境 `reset_mode="hard"`。
- 如何处理记忆：技能库（代码+描述+向量索引）是唯一持久化能力记忆，跨 episode 累积。
- 如何评测：Minecraft 科技树解锁数、独特物品数、探索距离。
- 真实证据：`voyager/voyager.py`（411 行）、`agents/curriculum.py`（498 行）、`agents/skill.py`（127 行）、`prompts/curriculum_task_decomposition.txt`（11 行）全部实读。
- 对 Atlas 的可借鉴点：技能=可执行代码+描述+检索的三元组与 Artifact 节点同构；把检索键从纯文本改成"描述+可执行性元数据"，图记忆才能真正参与规划。
- 不适合 Atlas 的地方：`progress` 整数游标 + "失败即跳过"意味着未完成目标被静默丢弃，与"可靠完成任务"直接冲突。
- 可信度：**高**（计划结构、失败处理、技能持久化三处均有代码直读证据）。

### SWE-agent
- 类型：论文 + GitHub｜年份：2024｜会议或来源：NeurIPS 2024（poster）
- 官方链接：https://arxiv.org/abs/2405.15793 ；代码 https://github.com/SWE-agent/SWE-agent
- 主要解决的问题：agent-计算机接口（ACI）设计本身决定软件工程任务成败。
- 核心机制：单 agent 循环 + 精心设计的命令集 + guardrails（禁危险动作、强制可解析输出）；失败由 reviewer 触发整轮 retry 并择优。
- 关键数据结构：**无计划结构**。`DefaultAgent` 持有 `self._trajectory: list`，每步 `StepOutput(action/observation/state/exit_status/done/submission)`。
- 执行流程：`run()` 里 `while not step_output.done:` → `step()` → `save_trajectory(choose=False)`；`done` 后交 `_rloop.on_submit(ReviewSubmission(...))`，若 `ScoreRetryLoop` 判 retry 则 `_next_attempt()` 并置 `done = False` 继续。
- 如何处理失败：三层——单步重试（`_RetryWithOutput`/`_RetryWithoutOutput` + `RetryAgent`）、动作拦截（`should_block_action` 抛 `_BlockedActionError`）、**整 episode 重跑**（`reviewer.py` retry loop，按分择优）。
- 如何处理状态变化：环境状态在容器内；`step.state = self.tools.get_state(env)` 每步快照。
- 如何处理记忆：轨迹 + `history_processors.py` 压缩；`last_trajectory_step["state"]["diff"]` 可提补丁。
- 如何评测：SWE-bench / Lite 的 % resolved。
- 真实证据：`sweagent/agent/agents.py`（1294 行实读）的 `DefaultAgent.run()`（约 413 行 `while not step_output.done`）、`handle_action()`、`handle_submission()`；`grep -niE "\bplan\b|dag|subtask|dependency"` 在该文件**零命中计划类结构**（仅 `CombinedAgentHook`、`circular dependency` 注释等无关片段）。
- 对 Atlas 的可借鉴点：`StepOutput` 六字段是 step 级最小模型的良好起点；`exit_status` 分类（submitted/exit_command/超时）可直接映射 Step 终态枚举。
- 不适合 Atlas 的地方：无计划无依赖；"重试=整 episode 重跑"长任务成本不可接受；reviewer 择优依赖可重复执行环境。
- 可信度：**高**（主循环与失败路径实读；"无计划结构"是 grep 验证的否定结论）。

### OpenHands
- 类型：论文 + GitHub｜年份：2024/2025｜会议或来源：ICLR 2025（arXiv Comments 原文 "Accepted by ICLR 2025"）
- 官方链接：https://arxiv.org/abs/2407.16741 ；代码 https://github.com/OpenHands/OpenHands （Python 内核已迁至 https://github.com/OpenHands/software-agent-sdk ）
- 主要解决的问题：通用软件开发者 agent 的平台化（沙箱、事件流、委派、可复现）。
- 核心机制：**事件流为唯一事实源** + 状态可序列化 + 沙箱；规划是"提示驱动的 PLAN.md 工作流"，非机器可读计划对象。
- 关键数据结构：`ConversationState`/`EventStore`（append-only 事件流）；**计划 = 工作区 markdown 文件** `.agents_tmp/PLAN.md`（默认路径，见 `context/prompts/sections/planning.py:95-96`）。
- 执行流程：`agent.step()` 一批动作 → 事件入流 → 下一轮；`/goal` 循环由 `conversation/goal/controller.py` 在每次 run 后 `judge_goal()` 判"继续/停止"，继续则发 followup 再跑。
- 如何处理失败：`conversation/stuck_detector.py`（515 行）检 4 类卡死——重复 action+observation、重复 action+error、agent 独白、交替模式；各自独立阈值（`StuckDetectionThresholds.action_observation/action_error/monologue/alternating_pattern`），命中注入 nudge。
- 如何处理状态变化：`ConversationState` + 事件流重放；`_truncate_at_finish()` 重放时截断到 `finish` 之后，避免重复副作用。
- 如何处理记忆：事件流 + 上下文压缩（`context/`）；`goal/` 提供跨 run 目标连续性判定。
- 如何评测：SWE-bench、GAIA、TheAgentCompany。
- 真实证据（实读）：`.../conversation/stuck_detector.py`（515 行，四模式+阈值）、`.../conversation/goal/controller.py`（135 行，"transport-agnostic brain of the /goal loop"）、`.../context/prompts/sections/planning.py`（134 行，Phase 1-4 提示工作流 + `PLAN.md`）、`.../agent/agent.py`（1700 行，`_truncate_at_finish`）。
- 对 Atlas 的可借鉴点：**`stuck_detector` 四模式+阈值是现成的重规划触发器蓝图**，比"连续 N 次无进展"更细，且四类对应不同处置；Atlas 的 `policies.py` 现只有 `Budget.max_repairs`，可直接扩成这套分类触发器。
- 不适合 Atlas 的地方：`PLAN.md` 是给人看的文本，无依赖解析、无 step 状态、无法机检；`/goal` 粒度是一次 run 而非 step。**"有 PLAN.md" ≠ "有规划能力"**。
- 可信度：**高**（关键文件逐一实读，"计划是 markdown"有代码注释与默认路径双重证据）。

### AutoGen
- 类型：论文 + GitHub｜年份：2023｜会议或来源：COLM 2024
- 官方链接：https://arxiv.org/abs/2308.08155 ；代码 https://github.com/microsoft/autogen
- 主要解决的问题：用"多 agent 对话"作统一编程范式，让可对话 agent 通过消息协作。
- 核心机制：actor 模型——agent 有 id，经 runtime 收发消息；订阅做主题路由；RPC 用 `send_message`，广播用 `publish_message`。
- 关键数据结构：**core 层无计划结构**。`SingleThreadedAgentRuntime` 持 `_message_queue: Queue[...]` 与 `_background_tasks`；`Subscription.is_match(topic_id)`/`map_to_agent(topic_id)` 决定投递。
- 执行流程：`_process_next()` 取 envelope，`match` 分派到 `_process_send`/`_process_publish`/`_process_response`，各自 `asyncio.create_task` 执行并 `task_done()`。
- 如何处理失败：core 把异常放 `_background_exception`，下次 `_process_next()` 或 `stop()` 时抛出；`_intervention_handlers.on_send` 可丢弃消息（`DropMessage`→`MessageDroppedException`）。
- 如何处理状态变化：agent 自持状态，runtime 无全局状态机。
- 如何处理记忆：`autogen_core/memory/`（可插拔协议）+ agentchat 层 `model_context`。
- 如何评测：论文以多任务案例为主，缺统一 benchmark。
- 真实证据（实读）：`autogen-core/src/autogen_core/_single_threaded_agent_runtime.py`（1029 行，`_message_queue`/`_process_next` 的 match 分派）；`_subscription.py` 的 `Subscription` 协议。**关键补充**：AutoGen 生态里真正的计划数据结构在 `autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py`（536 行）。
- 对 Atlas 的可借鉴点：消息信封（sender/recipient/future/message_id/cancellation_token）成熟，`PendingToolCall` 可照此建模；`map_to_agent` 思路可用于"把某 step 产物路由给下游 step"。
- 不适合 Atlas 的地方：每个 agent 自由对话**无法回答"完成了百分之几"**——没计划就谈不上偏差度量；当编排层会把 Atlas 状态机架空。
- 可信度：**高**（runtime 与订阅协议实读；"core 无计划结构"是文件级验证的否定结论）。

### LangGraph
- 类型：GitHub 项目（**本轮未检索到同行评审论文**）｜年份：2024 起持续维护｜会议或来源：无会议；README 自述 "Low-level orchestration framework for building, managing, and deploying long-running, stateful agents"
- 官方链接：https://github.com/langchain-ai/langgraph
- 主要解决的问题：为有状态长时运行 agent 提供图式编排 + 持久化 + 中断恢复 + 人类在环。
- 核心机制：**BSP 超步引擎**。`StateGraph` 静态声明节点与边（`add_node`/`add_edge`/`add_conditional_edges`/`set_entry_point`），`compile(checkpointer=...)` 得可执行图；每超步按 **channel 版本**决定哪些节点触发，执行后 `apply_writes` 更新版本并落 checkpoint。
- 关键数据结构：`Checkpoint`（`channel_versions: dict[str,int]`、`versions_seen`、`pending_writes`）、`PregelTask`。**无"计划节点"对象、无节点状态字段**——状态在 channel 值里。
- 执行流程：`_loop.py::tick()`：`step > stop → status="out_of_steps"`；`_prepare_tasks_and_emit_checkpoint()`；`if not self.tasks → status="done"`；`interrupt_before` 命中抛 `GraphInterrupt()`。`_algo.py::prepare_next_tasks()` 对 PULL 节点调 `_triggers(channels, channel_versions, versions_seen[name], ...)`。
- 如何处理失败：`_retry.py::RetryPolicy`（按节点配置）、`error_handler` 与 `_resume_error_handlers_if_applicable()`；中断靠 checkpoint 恢复。
- 如何处理状态变化：**最强处**——`_put_checkpoint(metadata)` 每超步持久化，`thread_id` 为恢复键；`compile()` docstring 明写 "allowing it to be paused, resumed, and replayed from any point"。
- 如何处理记忆：checkpointer 即短时记忆；长期记忆走 `BaseStore`。
- 如何评测：无统一 benchmark（框架），生态以 τ-bench / SWE-bench 集成样例为主。
- 真实证据（实读）：`_algo.py`（1459 行）`_triggers()`（1259）、`prepare_single_task()`（523）、`apply_writes()`（233）；`_loop.py`（2172 行）`tick()`（650）、`_put_checkpoint()`（1237）；`graph/state.py`（1978 行）`add_edge`（928）/`add_conditional_edges`（982）/`compile`（1177）；`langgraph/pregel/` 含 25 个文件。
- 对 Atlas 的可借鉴点：checkpoint 的"版本 + 已见版本"是幂等重放的成熟方案，可用于判断"该 step 输入是否变了、可否复用旧产物"；`interrupt_before` 语义可直接用于 pause/resume。
- 不适合 Atlas 的地方：**把计划埋进数据流**——拓扑写死在代码里，运行时无"计划对象"可诊断/修改/重排；"改计划"=改代码重启。且它**支持环**，不是 DAG 执行器。
- 可信度：**高**（调度与持久化两机制逐函数实读；但需明确它不是计划系统）。

### LLMCompiler
- 类型：论文 + GitHub｜年份：2023/2024｜会议或来源：ICML 2024
- 官方链接：https://arxiv.org/abs/2312.04511 ；代码 https://github.com/SqueezeAILab/LLMCompiler
- 主要解决的问题：ReAct 串行调用工具延迟高；把规划与执行解耦，让计划内独立工具并行。
- 核心机制：Planner（一次输出带 `$1/$2` 依赖占位符的任务列表）→ Task Fetching Unit（依赖满足即并行调度）→ Joiner（汇总并判定是否 replan）。
- 关键数据结构：**真 DAG**。`task_fetching_unit.py:42` `@dataclass class Task`：`idx/name/tool/args/`**`dependencies: Collection[int]`**`/observation/is_join`；`TaskFetchingUnit` 持 `tasks/remaining_tasks/tasks_done`（per-task `asyncio.Event`）。
- 执行流程：`llm_compiler.py` 的 `for i in range(self.max_replans)` → `planner.plan(is_replan=not is_first_iter)` → `TaskFetchingUnit.aschedule()/schedule()`，每轮 `_get_all_executable_tasks()` 取"所有依赖 done event 已 set"者，`asyncio.create_task` 并发 → `joinner` 判 `is_replan`，False 则 `break`。
- 如何处理失败：**无 step 级失败处理**。`_run_task` 直接 `await task()`，异常上抛；`aschedule` 的 `elif no_more_tasks and self._all_tasks_done(): break` 是唯一出口，**依赖环会永久空转**（`await asyncio.sleep(SCHEDULING_INTERVAL)`）。
- 如何处理状态变化：`_replace_arg_mask_with_real_value()` 运行前把 `${1}` 换成上游 `observation`——依赖解析即数据流解析。
- 如何处理记忆：无跨轮记忆；replan 时把已完成任务上下文塞进 prompt。
- 如何评测：HotpotQA / Movie Recommendation / ParallelQA 的延迟与准确率。
- 真实证据（实读）：`task_fetching_unit.py`（165 行）的 `Task` 定义、`_get_all_executable_tasks()`、`schedule()`、`aschedule()`、`_replace_arg_mask_with_real_value()`；`llm_compiler.py`（313 行）的 `max_replans` 循环与 `joinner`；`planner.py`（282 行）。全内存，**无 checkpoint**。
- 对 Atlas 的可借鉴点：**最接近 Atlas 需要的 Step/Dependency 模型**——`dependencies` 是整数索引引用，`$n` 是数据依赖形式化，`_get_all_executable_tasks()` 是可照搬的就绪判定；Atlas 应把 `dependencies` 升级成显式 `DependencyRow`（含 kind: data/order/verify）并入图时做环检测。
- 不适合 Atlas 的地方：**重规划是整计划重写**，无"只改坏掉那条链"；无持久化、无失败恢复、无环检测；`max_replans` 是固定次数而非信号驱动。
- 可信度：**高**（DAG 与调度器实读；"无环检测/无失败处理"为逐路径确认的否定结论）。

### ADaPT
- 类型：论文｜年份：2023/2024｜会议或来源：NAACL 2024 Findings（Comments 原文 "NAACL 2024 (findings) camera-ready"）
- 官方链接：https://arxiv.org/abs/2311.05772
- 主要解决的问题：计划粒度应"按需"——先试最粗粒度，失败再递归分解，避免为简单子任务付出过度分解成本。
- 核心机制：递归 as-needed decomposition——Executor 尝试当前子任务，失败则带失败上下文把该子任务递归拆小，直到成功或达最小粒度。
- 关键数据结构：递归调用栈 + 按需生长的子任务树（非预先展开）。
- 执行流程：Planner 出高层子任务 → Executor 逐个执行 → 失败节点递归调 Planner 分解 → 子任务完成后回父层。
- 如何处理失败：**失败驱动分解**——这是它相对 LLMCompiler 的关键差异：重规划粒度是失败的那个子任务，而非整计划。
- 如何处理状态变化：子任务栈保存中间结果供父层继续。
- 如何处理记忆：失败上下文向上传递，无长期记忆。
- 如何评测：ALFWorld、WebShop 等长程交互任务。
- 真实证据：**仅论文机制**。本轮未 clone 其代码仓库（未确认官方实现地址），工程实现标"待核实"。
- 对 Atlas 的可借鉴点：**"失败才分解"是抑制计划过早固化的正解**——PLANNING 阶段不必展开全部 step，只在 step 失败且重试耗尽时展开子 Step，直接落到 `Step.parent_step_id` 自引用。
- 不适合 Atlas 的地方：递归深度无界会 token 爆炸（论文用最小粒度兜底）；分解质量全靠 LLM，无 success predicate 校验。
- 可信度：**中**（机制清晰且被广泛引用，但无代码实证，递归终止条件无法确认）。

### DEPS
- 类型：论文｜年份：2023｜会议或来源：NeurIPS 2023（Crossref container-title "Advances in Neural Information Processing Systems 36"）
- 官方链接：https://doi.org/10.52202/075280-1480
- 主要解决的问题：开放世界多任务 agent 的计划常"想当然"，与实际物品/环境可用性脱节，需描述-解释-计划-选择闭环。
- 核心机制：**Describe**（目标描述器把任务翻成可理解指令）→ **Explain**（LLM 解释失败原因给反馈）→ **Plan**（生成/修正计划）→ **Select**（多候选选优）；Explain+Select 构成自我纠正环。
- 关键数据结构：候选计划集合（多计划并行生成再选优）。
- 执行流程：目标 → 描述 → 生成多个候选 → 执行 → 失败则 Explain 产出反馈 → 重新 Plan → Select 择优。
- 如何处理失败：**显式"解释失败"环节**是核心差异；反馈写回计划生成 prompt。
- 如何处理状态变化：依赖环境观测（开放物品/位置可用性）约束计划。
- 如何处理记忆：短期反馈记忆（解释文本）。
- 如何评测：Minecraft 开放世界任务（与 Voyager 同域，更强调计划纠错）。
- 真实证据：**仅论文机制**（venue 与标题经 Crossref 核验；本轮未读代码，因论文未指向可核的官方仓库），实现标"待核实"。
- 对 Atlas 的可借鉴点："Explain 失败原因"应成为 Schema 字段而非自由文本：`REPAIRING` 状态现在缺"失败归因"输入，DEPS 提示归因应结构化（哪一步、期望什么、实际什么、属哪类偏差）。
- 不适合 Atlas 的地方：多候选并行成本高；选优靠 LLM 比较缺可验证信号；无 step 级持久化。
- 可信度：**中**（venue 已核；机制无代码佐证，属"论文声称"层）。

### ReWOO
- 类型：论文｜年份：2023｜会议或来源：arXiv preprint（CoRR，无会议信息）
- 官方链接：https://arxiv.org/abs/2305.18323
- 主要解决的问题：ReAct 把观测塞回 prompt 导致 token 二次方增长；把推理与观测解耦，一次规划、批量取数、最后求解。
- 核心机制：Planner 生成带变量引用的计划（`#E1 = Tool[args]`）→ Worker 顺序执行填变量 → Solver 用全部证据一次作答。
- 关键数据结构：**带变量引用的线性计划**（一串 `#E1 = ...` 赋值，隐式形成依赖链）。
- 执行流程：Plan → 按序执行每个 `#Ek` → 用 `#E1..En` 拼最终 prompt → Solver。
- 如何处理失败：无（计划一次生成不 replan）；论文卖点正是"不用 replan 也够便宜"。
- 如何处理状态变化：变量表 `#E1..#En` 即状态，工具输出写入变量。
- 如何处理记忆：无跨任务记忆。
- 如何评测：HotpotQA、TriviaQA、SportsUnderstanding 的 token 消耗与准确率。
- 真实证据：**仅论文机制**；本轮未核代码（论文提供的是 prompt 模板，非系统）。
- 对 Atlas 的可借鉴点：**"变量引用"是依赖建模的最轻量形式**——比 DAG 便宜、比线性强；Step 可允许 `inputs: {name: "${stepID.output.field}"}`，调度前解析，等价于 `$n` 但更细（可到字段）。
- 不适合 Atlas 的地方：一次性计划=过早固化的极端形态；无失败恢复；工具必须幂等可批量。
- 可信度：**中**（机制清楚，无代码实证；"无 replan"是设计取舍不是能力）。

### HuggingGPT
- 类型：论文 + GitHub｜年份：2023｜会议或来源：NeurIPS 2023（poster）
- 官方链接：https://arxiv.org/abs/2303.17580 ；代码 https://github.com/microsoft/JARVIS
- 主要解决的问题：把 LLM 当控制器，编排 Hugging Face 专家模型完成多模态复合任务。
- 核心机制：Task Planning（生成结构化任务列表，每项含 `id/task/args/dep`）→ Model Selection → Task Execution（按依赖并发）→ Response Generation。
- 关键数据结构：**JSON 任务图** `{"id": n, "task": "...", "args": {...}, "dep": [ids]}`，无依赖用 `[-1]` 哨兵。
- 执行流程：`tasks = unfold(tasks)` → `tasks = fix_dep(tasks)` → `while True:` 扫描 `tasks`，对每项先做**依赖净化** `if dep_id >= task["id"]: task["dep"] = [-1]; break`（**前向/循环依赖被静默丢弃**）→ 满足依赖者起 `threading.Thread(target=run_task, ...)` → 主循环 `time.sleep(0.5)` 轮询，`retry > 160` 超时退出 → `thread.join()`。
- 如何处理失败：**几乎没有**。任务线程失败不重试，依赖方拿到缺失结果；`fix_dep` 只做依赖合法性兜底。
- 如何处理状态变化：依赖产物经 `results[dep_id]` 回填，`<GENERATED>-<id>` 占位符替换成上游输出（`run_task` 里 text/image/audio 三段替换）。
- 如何处理记忆：会话级 `messages` 多轮上下文。
- 如何评测：论文用任务成功率与人类偏好；同仓库 TaskBench（`generate_graph.py`/`graph_sampler.py`）做任务图合成评测。
- 真实证据（实读）：`hugginggpt/server/awesome_chat.py`（1070 行）的 `fix_dep()`（268）、`unfold()`（281）、`resource_has_dep()`（261）、`run_task()`（720）与上述调度循环；`git ls-tree` 确认同仓库有 `taskbench/generate_graph.py` 等——"任务图"是该项目的核心抽象。
- 对 Atlas 的可借鉴点：`dep` 哨兵 + 占位符替换是极其廉价的依赖机制，适合作 Atlas 第一版 Dependency 的最小实现；TaskBench 图采样器可用于造"DAG 形状"评测用例。
- 不适合 Atlas 的地方：**用"丢弃非法依赖"保证无环是反模式**，把计划错误变成静默降级；`threading` 无取消无 join 超时无回溯；无任何持久化。
- 可信度：**高**（依赖处理与调度循环实读，含关键 `dep_id >= task["id"]` 兜底）。

### Magentic-One
- 类型：论文 + GitHub（实现位于 autogen 的 agentchat 层）｜年份：2024｜会议或来源：**arXiv preprint（会议归属待核实）**
- 官方链接：https://arxiv.org/abs/2411.04468 ；代码 https://github.com/microsoft/autogen
- 主要解决的问题：通用多 agent 系统在开放任务上失控——不收敛、原地打转、目标漂移。
- 核心机制：**双账本**。Task Ledger 存 `facts` 与 `plan`；Progress Ledger 每轮回答五问：`is_request_satisfied`、`is_progress_being_made`、`is_in_loop`、`instruction_or_question`、`next_speaker`。
- 关键数据结构：`self._facts: str`、`self._plan: str`（**字符串，非结构图**）；progress ledger 是强制 schema 的 dict（缺 key 即 `key_error` 重试，上限 `_max_json_retries`）。
- 执行流程：外层 `_prepare_task_ledger()`（先问 facts 再据 facts 出 plan）→ 内层 `_orchestrate_step()` 每轮生成 progress ledger → `is_request_satisfied` 则 `_prepare_final_answer()`；否则更新 stall 计数。
- 如何处理失败：**全谱系唯一阈值化重规划触发器**：`if not is_progress_being_made: _n_stalls += 1`；`elif is_in_loop: _n_stalls += 1`；`else: _n_stalls = max(0, _n_stalls - 1)`；`if _n_stalls >= _max_stalls:` → 打印 "Stall count exceeded, re-planning with the outer loop..." → `_update_task_ledger()`（重写 facts+plan）→ `_reenter_outer_loop()`。
- 如何处理状态变化：`_facts` 是环境状态的文本摘要，每轮 `_update_task_ledger` 时刷新。
- 如何处理记忆：`_thread_to_context()` 把消息线程压成上下文；ledger 是跨轮记忆载体。
- 如何评测：GAIA、AssistantBench、WebArena（论文口径）。
- 真实证据（实读）：`_magentic_one_orchestrator.py`（536 行）的 `_orchestrate_step()` stall 计数与 `_max_stalls` 分支（约 380-405 行）、`_get_progress_ledger_prompt()`、`_update_task_ledger()`；目录含 `_magentic_one_group_chat.py`/`_magentic_one_orchestrator.py`/`_prompts.py`。
- 对 Atlas 的可借鉴点：**stall 计数 + 迟滞（`max(0,n-1)` 衰减）** 抗抖动，可直接用于"连续 N 次无进展"判据；Progress Ledger 五字段是"重规划前必须先回答的问题清单"，`REPAIRING` 应要求同样的自检字段。
- 不适合 Atlas 的地方：`facts`/`plan` 全是**自由文本**，无法做依赖校验、无法判断"哪一步失败了"；重规划粒度是**整账本重写**；ledger 依赖 LLM 稳定输出 JSON，重试耗尽即抛 `ValueError`。
- 可信度：**高**（stall 触发与重规划路径实读；venue 标注为待核实的 arXiv preprint）。

---

## 3. 三栏对照表（论文机制 / 工程实现 / 仅宣传）

| 工作 | ① 论文提出 | ② 工程真正实现（文件:符号） | ③ 仅宣传/概念层 | 证据强度 |
|---|---|---|---|---|
| ReAct | Thought-Action-Observation 交替 | `hotpotqa.ipynb::webthink()` 固定 7 轮 + prompt 拼接 | "可控推理"实为少量 few-shot 模板 | 高 |
| Plan-and-Solve | 两段式提示 | **未定位官方仓库**（`hengjui/Plan-and-Solve` 不存在） | 全部 | 中 |
| ToT | 树搜索（BFS+DFS） | 仅 `src/tot/methods/bfs.py`（beam，无节点对象）；**DFS 未开源** | "树"实为 beam，回溯仅论文层 | 高 |
| LATS | MCTS + 反思 | `hotpot/lats.py::Node/select_node/lats_search` 完整树+剪枝+回传 | "统一推理-行动-规划"的话语层 | 高 |
| Reflexion | 语言化强化学习 | `main.py` 整局循环 + `generate_reflections.py::update_memory` | "无需梯度即自我改进"的强表述 | 高 |
| SayCan | LLM 分 × affordance 分 | **未实读**（`google-research` saycan 子树未 clone） | 全部 | 中 |
| Voyager | 自动课程+技能库+迭代提示 | `voyager.py::inference`（扁平 sub_goals）、`skill.py`（json+向量库）、`curriculum.py` 失败即跳过 | "终身学习"实为单次长跑内技能累积 | 高 |
| SWE-agent | ACI 决定性能 | `agents.py::DefaultAgent.run` 线性循环 + `reviewer.py` 整轮重试 | "自主修复"= reviewer 择优 | 高 |
| OpenHands | 通用开发平台 | 事件流 + `stuck_detector.py` 四模式 + `goal/controller.py` | **"规划"= 提示工作流 + `.agents_tmp/PLAN.md`**，无机器可读计划 | 高 |
| AutoGen | 多 agent 对话编程 | `_single_threaded_agent_runtime.py::_process_next`（actor 队列）；计划只在 Magentic-One 文本账本 | "conversable agent 可完成任意任务" | 高 |
| LangGraph | —（无论文） | `_algo.py::_triggers/prepare_next_tasks`、`_loop.py::tick/_put_checkpoint` | **常被当"DAG 编排"**，实为可含环的超步引擎；计划能力在更高层包（README 指向 Deep Agents） | 高 |
| LLMCompiler | 规划执行解耦+并行 | `task_fetching_unit.py::Task.dependencies` + `_get_all_executable_tasks` | "编译器"隐喻大于实际（无 IR、无优化 pass） | 高 |
| ADaPT | 按需递归分解 | **未核实**（未 clone） | 全部 | 中 |
| DEPS | 描述-解释-计划-选择 | **未核实**（未 clone） | 全部 | 中 |
| ReWOO | 计划-取数-求解解耦 | **未核实**（无官方系统仓库） | 全部 | 中 |
| HuggingGPT | 任务规划+模型选择+执行 | `awesome_chat.py::fix_dep/unfold/run_task`（dep 数组+线程调度） | "通用任务求解"实为固定任务类型模板 | 高 |
| Magentic-One | Task/Progress 双账本 | `_magentic_one_orchestrator.py::_orchestrate_step`（stall 阈值触发重规划） | 账本是自由文本非结构计划 | 高 |

**读表要点**：② 栏标"未核实"的 5 项（Plan-and-Solve、SayCan、ADaPT、DEPS、ReWOO）不得再被当作"已实现机制"引用。③ 栏最需警惕两条：OpenHands 的 PLAN.md（有文件≠有计划）、LangGraph 的 DAG 叙事（有图≠DAG 调度）。**唯一"论文结构==代码结构"的是 LATS（树）与 LLMCompiler（DAG）**，是 Atlas 数据模型最可靠的两个参照物。

---

## 4. 重规划触发与计划可执行性判定

### 4.1 触发条件清单（信号 → 粒度 → 动作）

Atlas 现有 `orchestration/policies.py` 只有 `Budget.max_repairs/max_rounds/max_tokens/max_latency_ms/max_cost_usd` 与 `exhausted()`，粒度是 task。下表细到 step。

| # | 信号 | 检测判据 | 粒度 | 动作 | 依据 |
|---|---|---|---|---|---|
| T1 | 工具**确定性错误**（参数非法、路径不存在、schema 失败） | `error.kind == "invalid_input"` | step 重试 | 换参数重试，最多 `max_step_retry` | SWE-agent `_RetryWithOutput` |
| T2 | **瞬时错误**（超时、限流、5xx） | `error.kind == "transient"` | step 重试（退避） | 指数退避，幂等键不变 | LangGraph `_retry.py::RetryPolicy` |
| T3 | 同一 (action, observation) 对**重复 N 次** | 哈希最近 N 步判重 | step 重试→**换策略** | 先改参数再换工具；两次失败升级 T6 | OpenHands `_is_stuck_repeating_action_observation` |
| T4 | 同一失败 action **连续 K 次** | `_action_error_streak` 计数 | **换策略** | 禁用该 action/tool | OpenHands `_is_stuck_repeating_action_error` |
| T5 | **验证失败**（success predicate 为假但无异常） | `verify(step)` 显式判定 | **子目标重规划** | 只重写该 step 及其下游输入绑定，不碰无关分支 | 本文结论 3（无人实现，Atlas 可占位） |
| T6 | **连续 N 次无进展** | 无新事实/新产物 `+1`，否则 `-1`（迟滞） | **子目标→整计划** | `≥soft` 子目标级；`≥hard` 整计划重写并记 Decision | Magentic-One `_n_stalls`/`_max_stalls` |
| T7 | **状态不一致**（前置条件失效/依赖产物被外部改） | 执行前重算 `precondition_check` 并与计划假定比对 | **子目标重规划**（回溯到最近稳定 step） | 下游全置 `stale`，从该点重展开 | 结论 7：SayCan 的"每步重算"应落此 |
| T8 | **预算超限** | `Budget.exhausted()` 非空 | **整计划重写（降级）** | 切更粗粒度计划，弃可选分支，记 Decision | Atlas 现有 `Budget`；Magentic-One `_max_turns` |
| T9 | **依赖环/前向依赖**（生成期可检） | 拓扑排序失败 | **计划驳回（不执行）** | 打回 Planner 重生成，禁止静默丢依赖 | 反例：HuggingGPT `fix_dep`；LLMCompiler 无环检测致空转 |
| T10 | **success predicate 不可判定** | 校验期发现 `verify` 为空 | **计划驳回** | 要求补 verify 或显式标"需人工确认" | 本文结论 7 |

**粒度阶梯**（轻→重）：`step 重试 → 换策略 → 子目标重规划 → 整计划重写 → 计划驳回`。原则：能用轻的不用重的；每次升级必须留一条 Decision 节点，否则 replay 无法解释行为。

### 4.2 可执行性判定（五道闸，任一失败即回 PLANNING）

1. **前置条件可满足**：每步声明 preconditions（工具/文件/凭据/外部状态），且在计划生成时能对当前环境求值成 真/假/未知；"未知"必须配一个探测 step 作首个前置动作。
2. **依赖无环**：拓扑排序失败即驳回（T9）；禁止前向引用（HuggingGPT 的反模式正是丢弃它）；`depends_on` 与 `produces` 需形状匹配，上游 `produces` 必须覆盖下游声明输入。
3. **每步有可验证 success predicate**：`verify` 必须可执行判定（文件存在且内容匹配/退出码 0/schema 通过/断言通过），分三档 `executable`（最优）/`heuristic`/`llm_judge`（须标低置信并允许人工推翻）；无 `verify` 者标 `needs_human`（T10）。
4. **预算可行**：`Σ 每步预估 ≤ 剩余 Budget`，且预留至少一次 hard 级重规划空间；预估缺失按历史同类 p50，连续 3 次超预估 2× 触发 T8。
5. **可观测**：每步 observation 必须落 event log 且可被 `verify` 读取（Atlas 已有 `storage/events.py` 与 `observability/tracing.py`，应把 step observation 升为一等字段）。

### 4.3 "必须重规划"与"重试即可"的分界

> **失败原因在"计划的前提"里 → 重规划；原因在"本次参数/环境瞬时状态"里 → 重试。**

满足任一即**必须**升级到重规划（不许继续重试）：失败信号指向某个 precondition 为假（T7）；连续两次重试后 `verify` 仍为 false 且两次 action 等价（参数空间已探尽）；失败 step 上下游出现**事实冲突**（新观测与图记忆 Fact 节点矛盾——这是 Atlas 图记忆独有的判据，其他工作都没有）；预算耗用超该 step 预估 3×（T8 局部版）。

---

## 5. 对 Atlas 的设计建议

### 5.1 Step / Dependency / Scheduler 最小数据模型

**Step（最小执行单元，一行一记录，可持久化）**
- 标识：`step_id`（建议 `{run_id}:{seq}`）、`plan_id`、`parent_step_id`（支持 ADaPT 式按需展开）。
- 结构：`title`、`kind ∈ {tool_call, llm_call, verify, human_gate}`、`inputs`（含 `${stepID.output.field}` 引用）、`outputs`（声明产出供下游匹配）、`preconditions`、`verify`（含 `mode`）、`budget`。
- 状态：`status ∈ {pending, ready, running, done, failed, stale, skipped, needs_human}` + `attempt` + `stall_score`（T6 迟滞）+ `failure_kind`（结构化归因，DEPS 的 Explain 落点）+ `idempotency_key`（Atlas 已有）。
- 关键取舍：**`verify` 必填**，无 verify 只能进 `needs_human`。这条约束直接消灭"计划看起来可执行、实际无法判定完成"的大部分问题。

**Dependency（边，独立表）**
- 字段：`from_step`、`to_step`、`kind ∈ {data, order, verify, retry_of}`、`binding`（data 边说明"from 的哪个输出字段 → to 的哪个输入参数"）。
- 入图强制不变式（对应 §4.2 第 2 条）：无环、无前向引用、`produces ⊇ consumes`、data 边 binding 非空。
- **不要复用 `EdgeRow`**：`EdgeRow` 是语义记忆（Constraint/Decision/Fact/Artifact 之间关系），Dependency 是控制流；混表会让 replay 无法区分"知识关系"与"执行顺序"。

**Scheduler（纯函数决策 + 显式写入）**
- 三个纯函数：`ready_steps(plan, state)`（order 依赖 done + data 依赖产物可用 + 前条件为真）、`next_action(ready, policy)`、`should_replan(plan, signals)`（§4.1 表）。
- 调度器**不执行**，只产"派发指令"；执行由 Worker 承担，结果回写 event log 再驱动下一轮。决策与副作用分离让调度器可离线单测——正好用上 Atlas 的 fixture + 故障注入评测。
- 并行度只来自 data 依赖的独立性（LLMCompiler 的结论）；order 依赖是串行的，不要为它引入并发。
- 每轮调度结束写 checkpoint（对齐 `tick()` 语义），但**内容是"计划 + 各 step 状态"**，不是通道版本——这是 Atlas 与 LangGraph 的关键差异。

**状态机（在 `TaskStateMachine` 之外增 StepStateMachine）**
- `pending → ready → running → {done, failed, needs_human}`；`failed → ready`（重试，attempt+1）；`failed → stale`（被上游重规划波及）；`stale → ready`（重绑输入后）；`done → stale`（T7 时已完成者也要失效重做）。
- 非法迁移必须 `record_violation`（Atlas 已有，`state_machine.py:220`），这是 replay 定位"谁改坏了状态"的关键证据。
- **Task 级状态机保留但降级**：`TaskState` 只表达"run 在哪个阶段"，推进由 step 状态聚合得出（全部 step done → 进 AUDITING），这样 `to_checkpoint/from_checkpoint` 不必大改。

### 5.2 可直接借鉴

| 借鉴对象 | 借什么 | 落到 Atlas 哪里 |
|---|---|---|
| LLMCompiler | `Task.dependencies` + `_get_all_executable_tasks()` 就绪判定；`$n` 升级为字段级 binding | `Dependency.binding` + `Scheduler.ready_steps` |
| LangGraph | checkpoint 的"版本+已见版本"用于幂等重放判定；`interrupt_before` 暂停语义 | `storage/repository.py` checkpoint 扩展；pause/resume |
| LATS | `Node.exhausted` + "回退到最近未耗尽祖先" | REPAIRING 的"回溯到稳定点" |
| OpenHands | `stuck_detector` 四模式+阈值（最完整的工程化触发器） | `policies.py` 扩 `ReplanPolicy` |
| Magentic-One | stall 计数带衰减抗抖动；重规划前的自检字段清单 | `should_replan()` 判据 + REPAIRING 必填字段 |
| SWE-agent | `StepOutput` 六字段 | `Step` 最小字段集 |
| Voyager | 技能=代码+描述+检索三元组 | `memory/schema.py` Artifact 加"可执行性元数据" |
| ADaPT | "失败才分解" | `Step.parent_step_id` + T5 的子目标展开 |
| ReWOO | 变量引用式依赖（比 DAG 轻） | `Step.inputs` 引用语法 |

### 5.3 不能照搬

1. **LangGraph 的"计划=代码里写死的图"**：Atlas 的计划必须是**数据**，能运行中被审计、局部修改、replay。照搬会丢掉核心卖点。
2. **Voyager 的"失败即跳过"**：与"可靠完成任务"直接冲突；失败必须落 `failed` 并触发 T5/T6，绝不静默前进。
3. **HuggingGPT 的"丢弃非法依赖"**：把计划错误变成静默降级，最坏的反模式；必须驳回（T9）。
4. **Reflexion / SWE-agent 的"整段重跑"**：长任务成本线性爆炸；只能借反思的**内容**（计划修正条目），不能借其**粒度**。
5. **ToT 的固定深度 beam**：`task.steps` 是离线谜题产物，长程无法预知深度；需自适应终止（verify + stall），不是固定步数。
6. **AutoGen core 的 actor 自由对话**：没有全局计划就没有"完成度"；可借信封/订阅做**传输层**，不可做**编排层**。
7. **LATS 的全内存 MCTS**：树的价值在回溯，代价是 token；Atlas 若要树搜索只能用**计划生成阶段**（选 1 个计划执行），不能用于执行期。

### 5.4 落地顺序（三步，每步可独立评测）

1. **Step 化**：把 `Orchestrator.solve()` 的单 subtask（`subtask_id="S1"` 默认值）改成多 step 循环；加 `StepRow` + `StepStateMachine`；`RunMetricRow.step_id` 已预留字段正好接上。评测：现有 fixture 的 replay 结果不变（回归）+ "第 k 步崩溃后续跑"新用例。
2. **依赖化**：加 `DependencyRow` + `Scheduler.ready_steps`；先只支持线性+并行（order/data），不做树搜索。评测：LLMCompiler 式并行任务时延对比 + 环检测拒绝用例。
3. **重规划协议**：实现 T1–T10 与 `should_replan()`；先做 T1/T2/T6/T8（有现成参照物），T5/T7 作为 Atlas 独有能力后做。评测：故障注入下"局部修复 vs 整体重跑"的 token/成功率对比——这是 Atlas 相对上述工作的可发表差异点。

---

## 6. 元数据核验表

| 简称 | 标题 | 年份 | venue（工具原样） | 链接 | 核验方式 |
|---|---|---|---|---|---|
| ReAct | ReAct: Synergizing Reasoning and Acting in Language Models | 2022/2023 | ICLR 2023 | https://arxiv.org/abs/2210.03629 | Lead 表（Comments："v3 is the ICLR camera ready version"） |
| Plan-and-Solve | Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models | 2023 | ACL 2023 | https://arxiv.org/abs/2305.04091 | Lead 表（Comments："ACL 2023"）+ 本轮 paper_search 复核 arXiv:2305.04091 |
| ToT | Tree of Thoughts: Deliberate Problem Solving with Large Language Models | 2023 | NeurIPS 2023（oral） | https://arxiv.org/abs/2305.10601 | Lead 表（OpenReview venue）+ 本轮 Crossref 复核 container-title「Advances in Neural Information Processing Systems 36」，DOI 10.52202/075280-0517 |
| LATS | Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language Models | 2023/2024 | ICML 2024 | https://arxiv.org/abs/2310.04406 | Lead 表（PMLR v235 TOC 命中）；**本轮未能独立复核**（arXiv 429） |
| Reflexion | Reflexion: Language Agents with Verbal Reinforcement Learning | 2023 | NeurIPS 2023（poster） | https://arxiv.org/abs/2303.11366 | Lead 表（OpenReview venue）+ 本轮 Crossref 复核「Advances in Neural Information Processing Systems 36」，DOI 10.52202/075280-0377 |
| SayCan | Do As I Can, Not As I Say: Grounding Language in Robotic Affordances | 2022 | CoRL 2022 | https://arxiv.org/abs/2204.01691 | Lead 表（PMLR v205 TOC）；本轮未独立复核 |
| Voyager | Voyager: An Open-Ended Embodied Agent with Large Language Models | 2023 | TMLR（2024 接收） | https://arxiv.org/abs/2305.16291 | Lead 表（OpenReview venue="Accepted by TMLR"） |
| SWE-agent | SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | 2024 | NeurIPS 2024（poster） | https://arxiv.org/abs/2405.15793 | Lead 表（OpenReview venue="NeurIPS 2024 poster"） |
| OpenHands | OpenHands: An Open Platform for AI Software Developers as Generalist Agents | 2024/2025 | ICLR 2025 | https://arxiv.org/abs/2407.16741 | **本轮独立核验**：abs 页 Comments 原文「Accepted by ICLR 2025; Code: this https URL」 |
| AutoGen | AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation | 2023 | COLM 2024 | https://arxiv.org/abs/2308.08155 | Lead 表（OpenReview venue="COLM"；另有「Submitted to ICLR 2024」=未录用，不作 venue） |
| LangGraph | （无同行评审论文） | 2024– | 无会议；README 自述 "Low-level orchestration framework…" | https://github.com/langchain-ai/langgraph | **本轮独立核验**：实读仓库 README 与代码；未检索到论文，**不作论文类条目引用** |
| LLMCompiler | An LLM Compiler for Parallel Function Calling | 2023/2024 | ICML 2024 | https://arxiv.org/abs/2312.04511 | Lead 表（Comments："ICML 2024"） |
| ADaPT | ADaPT: As-Needed Decomposition and Planning with Language Models | 2023/2024 | NAACL 2024 Findings | https://arxiv.org/abs/2311.05772 | **本轮独立核验**：abs 页 Comments 原文「NAACL 2024 (findings) camera-ready」 |
| DEPS | Describe, Explain, Plan and Select: Interactive Planning with LLMs Enables Open-World Multi-Task Agents | 2023 | NeurIPS 2023 | https://doi.org/10.52202/075280-1480 | **本轮独立核验**：Crossref container-title=「Advances in Neural Information Processing Systems 36」，cited 35 |
| ReWOO | ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models | 2023 | arXiv preprint | https://arxiv.org/abs/2305.18323 | Lead 表（Comments 无会议信息） |
| HuggingGPT | HuggingGPT: Solving AI Tasks with ChatGPT and its Friends in Hugging Face | 2023 | NeurIPS 2023（poster） | https://arxiv.org/abs/2303.17580 | Lead 表（OpenReview venue="NeurIPS 2023 poster"） |
| Magentic-One | Magentic-One: A Generalist Multi-Agent System for Solving Complex Tasks | 2024 | **arXiv preprint（会议归属待核实）** | https://arxiv.org/abs/2411.04468 | **本轮独立核验**：abs 页「Submitted on 7 Nov 2024」，Comments 字段为空 |

### 6.1 本轮"待核实"清单

1. **LATS 的 ICML 2024**——采信 Lead 表 PMLR v235 TOC；本轮 PMLR/arXiv 因限流或不可达未独立复核。
2. **SayCan 的 CoRL 2022**——采信 Lead 表；未独立复核，且官方代码（`google-research` saycan 子树）**未实读**，"分数相乘"仅论文层。
3. **Plan-and-Solve 的工程实现**——`https://github.com/hengjui/Plan-and-Solve` 返回 `repository not found`，**官方仓库未定位**，全部机制归"论文主张"。
4. **ADaPT 的工程实现**——未 clone 未读代码；"失败驱动递归分解"的终止条件仅据论文描述。
5. **DEPS 的工程实现**——venue 已核，但本轮未读代码，"Explain→Plan→Select"闭环实现细节未核实。
6. **ReWOO 的工程实现**——无官方系统仓库可核，仅论文机制。
7. **Magentic-One 的会议归属**——abs 页 Comments 为空，仅确认 2024-11-07 提交；venue 写 arXiv preprint。
8. **ToT 的 DFS**——仓库 `src/tot/methods/` 只有 `bfs.py`；论文 DFS 是否有未公开实现**无法确认**，"DFS 可剪枝"只能算论文主张。
9. **LangGraph 是否有同行评审论文**——本轮 paper_search 未命中，按"工程框架"处理；若后续发现论文需回填 §6 第 11 行。
