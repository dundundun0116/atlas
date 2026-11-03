# RQ3 环境状态感知与变化适应

> 调研范围：环境状态读取、工具返回变化、外部网页/数据库/文件系统变化、工具调用失败、**工具调用已提交但客户端断线（unknown commit / in-doubt）**、环境与内部记忆不一致、observation→action→verification 闭环、执行后重新感知、过期信息检测。
> 证据规则：元数据一律来自本会话工具返回（OpenReview API、arXiv abs 页、`notes/research-2026-11/venue-verified.md`）；实现证据一律来自本会话实际抓取的 GitHub 源码；工程实践来源为本会话实际访问并抓取正文的官方文档。
> 抓取时间：2026-11-04。所有「代码级证据」的仓库与文件路径均已实测存在（HTTP 200 + 正文命中）。
> 三栏约定：**【论文机制】**= 论文里声明的机制；**【工程实现】**= 仓库里能定位到文件与关键语句的实现；**【仅宣传】**= README/博客声明但代码中未找到对应机制。

---

## 0. 结论摘要（8 条硬结论）

1. **没有一个被调研的 agent 系统在 agent 侧维护"环境状态模型"。** 18 个工作里，环境状态要么由环境自身持有（网站/VM/DB/容器），要么根本不被持有。OSWorld 的 VM snapshot 是真状态快照，但它只用于 `reset()` 复原，从不参与推理；τ-bench 与 AppWorld 的"状态模型"在**评测器**一侧（DB 哈希、state-based unit test），不在 agent 一侧。这是 Atlas 可以正当占位的空白。

2. **"每步重新观测"是压倒性主流（11/18）。** WebArena 的 `ScriptBrowserEnv.step()` 每步调用 `_get_obs()` 从活的页面重新抓 DOM/accessibility tree；BrowserGym、OSWorld、SWE-agent、Voyager 同构。这意味着它们的"状态感知"实际上是"状态重读"，没有任何观测缓存与一致性检查——一旦重读失败或读到的比上次旧，系统无从发现。

3. **只有 BrowserGym 一个系统做了"步骤级验证闭环"。** `BrowserGym.post_step()` 每步调 `_task_validate()`，并且**在验证前后备份/恢复 `page` 与 `page_history`**，注释原文："back-up these in case validate() navigates pages and messes the history"、"A recovery fix will be applied"。这是"验证动作本身必须无副作用"的工程实证，可直接借入 Atlas 的"执行后重新感知"。

4. **"动作已提交但结果未知"在这些基准里完全没有建模。** 逐条：WebArena 把异常写进 `info["fail_error"]` 且 `terminated=False` 继续；τ-bench 把异常 `str()` 成 observation 回喂模型；BrowserGym 存 `last_action_error` 字符串并特判 `TimeoutError`；SWE-agent 用 `check ∈ {ignore,warn,raise}`，`raise` 时先 `close()` 销毁环境再抛；OSWorld 的 `step()` 连 `try` 都没有，异常直接冒泡。**没有任何一个系统有 in-doubt 状态、幂等键、去重或对账。**

5. **唯一有"失败分类"的是 AgentBench，但它的分类恰好漏掉了未知提交。** `src/client/task.py` 里 `class TaskError(enum.Enum)` 五类：`START_FAILED / INTERACT_FAILED / AGENT_FAILED / NETWORK_ERROR / NOT_AVAILABLE`。关键缺陷：`/interact` 请求发生网络异常时，代码**直接返回 `NETWORK_ERROR` 且不调用 `/cancel`**（其它分支都调了），此时 `session_id` 是已知的、服务端可能已经执行，客户端却既不重试也不查询——这是文献与代码里最接近 "unknown commit" 的场景，而它被当成"错误"处理掉了。

6. **成熟度最高的 OpenHands 也只有"内存版" PendingToolCall。** `AgentController._pending_action_info: tuple[Action, float] | None`（action + 时间戳）只存在于控制器对象上；超时 60s 只写日志，代码注释原文 "just for logging purposes"、"don't clear it"；`State` 数据类里**没有任何 pending 字段**（`grep -i pending openhands/controller/state/state.py` 无命中），因此 checkpoint/restore 后这条信息丢失。它唯一的部分补偿是 `_is_awaiting_observation()`：倒序扫事件流找最近的 `AgentStateChangedObservation` 反推"在等观测"——用日志反推代替持久化状态，正是 Atlas 要避免的"第二真源"式推断。

7. **持久化执行（durable execution）已经把这个问题解透了，agent 社区没接。** Temporal 用 Event History 做重放、用 Schedule-To-Start / Start-To-Close / Schedule-To-Close 三种超时 + Heartbeat 把"worker 死了但 Activity 可能已执行"变成可判定事件；DBOS 的 "If a workflow is interrupted for any reason, DBOS automatically recovers its execution from the last completed step"；saga 给补偿、outbox 给"意图先落盘再发消息"、fencing token 给"过期持有者不得写"。这些是 Atlas `PendingToolCall` 的现成设计语汇。

8. **Atlas 现有幂等键设计与幂等语义相反，是必须先修的 bug 级问题。** `atlas/tools/protocol.py::build_idempotency_key(session_id, subtask_id, attempt=1)` 把 `attempt` 编进键里：同一逻辑请求第 2 次尝试得到**不同**的键，服务端无法据此去重，等于每次重试都是一次新的副作用。IETF 草案原文："The idempotency key MUST be unique and MUST NOT be reused with another request with a different request payload." 键必须绑定"逻辑请求"，attempt 只能作为独立计数字段。

---

## 1. 状态维护光谱（四档归类）

分档标准：**(a)** 系统内存在显式环境状态表示（world state / state graph / 观测缓存），且存在一致性检查或版本比对；**(b)** 无状态，每次动作前后重新观测；**(c)** 有明确的 observation→action→verification 闭环（验证在环内，不是事后判分）；**(d)** 声明有状态、实现里没有（声明与实现落差）。

| 档 | 系统 | 关键代码/论文证据 | 判定 |
|---|---|---|---|
| **a** | 无（agent 侧） | — | 空白 |
| **a（环境/评测器侧）** | OSWorld | `desktop_env/desktop_env.py`：`snapshot_name="init_state"`、`_revert_to_snapshot()`、`is_environment_used` 标志；**状态快照只服务 reset** | 有状态，但与推理脱钩 |
| **a（评测器侧）** | τ-bench | `tau_bench/envs/base.py`：`self.data` 为内存数据库；`get_data_hash()` = `sha256` of `to_hashable(self.data)`；`calculate_reward()` 用 GT 动作重放后比哈希 | 有状态模型，属评测器 |
| **a（评测器侧）** | AppWorld | `src/appworld/environment.py`：`evaluate()`→`evaluate_task()` + `task.ground_truth`；DB 在 `data/tasks/<id>/dbs`；`api_calls.jsonl`+`environment_io.md` 全量落盘 | 有状态+有审计，属评测器 |
| **a（评测器侧）** | TheAgentCompany | 每任务 docker 镜像含 `/utils/init.sh`、`/utils/eval.py`、"Subcheckpoints checking (secondary)" | 有状态+子检查点 |
| **b** | WebArena | `browser_env/envs.py`：`step()` 内 `observation = self._get_obs()`；env 自身只存 `reset_finished` | 纯重观测 |
| **b** | BrowserGym | `browsergym/core/src/browsergym/core/env.py`：`post_step()` 重新 `extract_dom_snapshot`；`page_history` 仅 tab 栈 | 纯重观测（但有闭环，见 c） |
| **b** | OSWorld（推理路径） | `step()` 末尾 `observation = self._get_obs()`（截图） | 纯重观测 |
| **b** | SWE-agent | `sweagent/environment/swe_env.py::communicate()`：命令 → `r.output`，无状态对象 | 纯重观测（容器即状态） |
| **b** | Voyager | `voyager/env/bridge.py::step()` 转发 `/step`；`has_reset` 守卫 + `pause/unpause` | 纯重观测 |
| **b** | AgentBench（client） | `src/client/task.py::run_sample()`：`while RUNNING` 轮询，每轮拿全量 `history` | 状态在服务端，client 无状态 |
| **b** | ReWOO | Planner 一次性出完整 plan（含变量占位），Worker 顺序取观测 | 刻意"去观测"，最极端（b） |
| **c** | **BrowserGym** | `post_step()`→`_task_validate()`；`prev_active_page/prev_page_history` 备份恢复 | **唯一每步验证** |
| **c** | Voyager | `voyager/agents/critic.py`：读 `events` 列表，任一 `onError` 事件即 `return None`；从 `events[-1]` 的结构化 observation 判定 | 闭环但 LLM 判定 |
| **c（弱）** | τ-bench | 仅终局 `done` 时 `calculate_reward()` | 事后，不在环内 |
| **c（弱）** | Reflexion / LATS | 反馈在 trial 之间（Reflexion 的 episodic memory buffer；LATS 的 MCTS + value function + self-reflection） | 试验间闭环 |
| **d** | **OpenHands** | `_pending_action_info` 内存态 + 60s 仅日志；`State` 无 pending 字段 | 声称跟踪、不可恢复 |
| **d** | 各基准的"stateful environment"叙述 | 环境自身有状态（网站/VM/DB），但 agent 侧零状态建模；引用时常被当作 agent 能力 | 概念混淆 |

**光谱判读**：这条光谱的横轴其实是"状态放在谁那里"。放环境那里（WebArena/OSWorld/SWE-agent）→ agent 无法判断自己看到的与真实的是否一致；放评测器那里（τ-bench/AppWorld）→ 只有判分能力，没有在线纠错能力；放 agent 那里 → **目前只有 OpenHands 尝试过，且没落盘**。Atlas 要占的是第四格。

---

## 2. 逐工作分析

### WebArena
- **类型**：论文 + GitHub 项目（论文机制 + 工程实现）
- **年份**：2023（arXiv）/ 2024（会议）
- **会议或来源**：ICLR 2024（poster）— OpenReview `content.venue.value`，见第 6 节
- **官方链接**：[arXiv 2307.13854](https://arxiv.org/abs/2307.13854)｜[web-arena-x/webarena](https://github.com/web-arena-x/webarena)
- **主要解决的问题**：在真实自托管网站上评测 web agent，替代此前的合成玩具环境。
- **核心机制**：Playwright 驱动的 `ScriptBrowserEnv`，四个自托管站点（gitlab / shopping / shopping_admin / forum / kiwix）+ 预置登录态；动作是字符串脚本，观测是 DOM 或 accessibility tree。
- **关键数据结构**：`Observation`（html / accessibility_tree / screenshot）、`ObservationMetadata`、`Trajectory`、`DetachedPage(url, content)`、`StateInfo`。
- **执行流程**：`reset(options={"storage_state": path})` → `setup()` 建浏览器上下文 → 循环 `step(action)`：`execute_action()` → 硬 `sleep` → `_get_obs()` → 返回 `(obs, float(success), False, False, info)`。
- **如何处理失败**：`try/except` 包住 `execute_action`，异常只写 `info["fail_error"] = str(e)`，`reward=0`，**`terminated=False`、`truncated=False`，回合继续**。没有失败分类、没有重试、没有 in-doubt 概念。
- **如何处理状态变化**：不处理。env 不含任何环境状态表示；`reset()` 只重建浏览器上下文，**不清后端数据库**。真实复原靠运维手工：`environment_docker/README.md` 的 "Environment reset" 明写 `docker stop/remove` 四个容器后 `docker run` 重建，且这句话的前置是 "After evaluating the 812 examples"——**全局 reset，不是每任务 reset**。
- **如何处理记忆**：无。跨步只有观测文本；无摘要、无状态缓存。
- **如何评测**：事后 `evaluation_harness/evaluators.py`：`StringEvaluator`（exact_match / must_include / fuzzy_match / ua_match）、`URLEvaluator`、`HTMLContentEvaluator`（`program_html`，用 JS locator 或 `func:` 取元素内容），`EvaluatorComb` 组合，`evaluator_router` 按 config 的 `eval.type` 分派。
- **真实证据**：【工程实现】`browser_env/envs.py:231-269`（step 全貌）、`evaluation_harness/evaluators.py:32-374`、`environment_docker/README.md:108-120`（reset 段）。**【论文机制】**自托管 + 812 任务的设定（README 的 "812 examples" 为一手出处）。
- **对 Atlas 的可借鉴点**：(1) 判分与执行解耦的 `evaluator_router` 模式——判分器是数据驱动的，可按任务声明式选配，正好对应 Atlas 的 audit 策略插件化；(2) `program_html` 用"页面里的真实元素内容"而非自然语言判断成功，是"读回验证"的廉价版。
- **不适合 Atlas 的地方**：env 无状态、reset 粗粒度、失败只有字符串——三件都是 Atlas 要明确**不做**的。跨任务共享后端 DB 意味着任务间状态污染，Atlas 的 replay 语义不能建立在这种环境上。
- **可信度**：**高**。源码逐段读过，reset 语义有 README 一手文字。

### BrowserGym
- **类型**：论文 + GitHub 项目
- **年份**：2024（arXiv）/ 2025（期刊）
- **会议或来源**：TMLR 2025 — OpenReview venue = "Trans. Mach. Learn. Res. 2025"。**注意标题是《The BrowserGym Ecosystem for Web Agent Research》，不是常引的 "A Gym Environment…"**（arXiv abs 页标题实测）
- **官方链接**：[arXiv 2412.05467](https://arxiv.org/abs/2412.05467)｜[ServiceNow/BrowserGym](https://github.com/ServiceNow/BrowserGym)
- **主要解决的问题**：把 WebArena / WorkArena / MiniWoB 等异构 web 任务统一到一个 gym 接口 + 统一 agent 接口，做可比评测。
- **核心机制**：`BrowserEnv(gym.Env)` 抽象基类；动作是"可执行 python 代码或函数调用"；观测是 DOM snapshot + 截图 + 结构化元数据；**每步 `validate()`**。
- **关键数据结构**：`page_history: dict`（tab 栈，`page -> None`）、`last_action`, `last_action_error`, `chat.messages`、`obs` dict。
- **执行流程**：`step(action)` → `pre_step()`（含 `send_message_to_user`、`report_infeasible_instructions`）→ `execute_python_code` → `post_step(info, validate=True)` → `_task_validate()` → 组 5 元组 `(obs, reward, terminated, truncated, info)`；`terminated = done or (terminate_on_infeasible and infeasible_message_received)`。
- **如何处理失败**：`except Exception as e: self.last_action_error = f"{type(e).__name__}: {e}"`，并对 `TimeoutError: Timeout ([0-9]+)ms exceeded.` 正则特判把 `action_exec_timeout` 秒数塞进 info。异常 → 观测文本，模型自己决定下一步。无分类、无重试。
- **如何处理状态变化**：**验证期的副作用防护**是本工作最硬的贡献：`_task_validate()` 先 `prev_active_page = self.page; prev_page_history = self.page_history.copy()`，调用 `self.task.validate(...)` 后若发现页面或历史被验证器改动，则强制恢复并打日志 "A recovery fix will be applied"。换句话说，它承认"验证会改变状态"，并把它当 bug 处理。
- **如何处理记忆**：不建模。`chat.messages` 是对话历史，不是环境状态。
- **如何评测**：任务对象自带 `validate(page, chat_messages) -> (reward, done, user_message, info)`，**每步调用**；`terminate_on_infeasible` 允许 agent 主动声明不可完成。
- **真实证据**：【工程实现】`browsergym/core/src/browsergym/core/env.py`：`step()`（含 `last_action_error` 与 TimeoutError 特判）、`post_step()`、`_task_validate()`（含备份恢复）。
- **对 Atlas 的可借鉴点**：**(1) 验证动作必须被当作"可能改变世界的动作"**——Atlas 做"执行后重新感知"时，重感知本身可能触发写操作（例如访问带计数的接口、刷新页面触发副作用），必须按 BrowserGym 的方式备份/隔离或改用只读观测通道。**(2) `terminate_on_infeasible`** 对应 Atlas 的"不可完成"显式上报，比让它继续瞎试便宜。
- **不适合 Atlas 的地方**：每步 validate 的代价是每步一次完整 DOM 抽取，长程任务下开销线性叠加；且它没有观测缓存，无法回答"我看到的是不是已经过期"。
- **可信度**：**高**。源码逐段读；标题与 venue 均实测。

### OSWorld
- **类型**：论文 + GitHub 项目
- **年份**：2024
- **会议或来源**：NeurIPS 2024（Datasets & Benchmarks Track poster）— OpenReview venue = "NeurIPS 2024 Track Datasets and Benchmarks Poster"
- **官方链接**：[arXiv 2404.07972](https://arxiv.org/abs/2404.07972)｜[xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld)
- **主要解决的问题**：在真实操作系统（虚拟机）上评测多模态 computer-use agent 的开放式任务。
- **核心机制**：`DesktopEnv(gym.Env)` 包住一个 VM（VMware / VirtualBox / Docker+KVM / AWS / Daytona），通过 controller 执行 pyautogui / computer_13 动作，观测为截图，**reset 靠 VM snapshot 回滚**。
- **关键数据结构**：`snapshot_name`（默认 `"init_state"`）、`is_environment_used`、`action_history`、`_traj_no`/`_step_no`、`setup_controller`、`evaluator{func, result, postconfig}`、`metric`/`result_getter`。
- **执行流程**：`reset(task_config)` → 若 `is_environment_used` 则 `_revert_to_snapshot()` + `_start_emulator()` → `setup_controller.setup(config)`（失败重试 `MAX_RETRIES`）→ `_get_obs()`；循环 `step(action)`：特殊动作 `WAIT/FAIL/DONE` 先处理 → `controller.execute_action()` → `sleep(pause)` → `_get_obs()`；最后 `evaluate()`。
- **如何处理失败**：`step()` **无 try/except**，`execute_action` 抛异常即向上冒泡（回合中断）。`reset()` 里的 setup 失败有 `MAX_RETRIES` 重试，且注释明写 "Setup may mutate the guest before failing. A later retry or task must restore the snapshot even if setup never completes"——**这是全篇最接近"部分成功"的处理**：承认 setup 可能已经改了 VM，因此落实"失败也要先回滚"。
- **如何处理状态变化**：唯一用真快照的系统。`snapshot_name="init_state"`，`is_environment_used` 用一个布尔量做"脏标记"，避免无谓 snapshot 操作（云 provider 上快照昂贵）。但快照只进不出：从不把快照读回来做"与 agent 认知比对"。
- **如何处理记忆**：`action_history` 只追加，用于 `infeasible` 类任务判定"最后一步是不是 FAIL"。
- **如何评测**：`evaluate()` = 跑 `postconfig` → 按 `evaluator["func"]` 走 `infeasible`（要求最后动作是 FAIL 才给 1）或 `result_getter` 取结果，`metric` 可以是列表多指标。`step()` 里 `reward=0`、`done=False` 是 `todo` 注解的占位——**执行期完全没有 reward 信号，判分完全事后**。
- **真实证据**：【工程实现】`desktop_env/desktop_env.py`：`reset()`（快照回滚 + `is_environment_used` + 重试）、`step()`（无异常捕获、`reward=0 done=False`）、`evaluate()`（infeasible / result_getter）。README 列出 VMware/VirtualBox/Docker/AWS/Daytona 五种 provider 与并行执行入口 `scripts/python/run_multienv.py`。
- **对 Atlas 的可借鉴点**：**(1) "脏标记 + 惰性回滚"**——不是每次 reset 都付快照代价，而是用 `is_environment_used` 决定是否回滚；Atlas 的 checkpoint 恢复可以同构：只有检测到副作用才回滚。**(2) `postconfig` 概念**——判分前允许再改一次环境（例如启动被检查的服务），说明"判分本身需要准备状态"，Atlas 的 audit 也需要显式的"评价前置配置"。
- **不适合 Atlas 的地方**：VM 级快照对 API/网页类工具不可用；`step()` 不捕异常意味着"动作已发出但结果未知"直接表现为进程崩溃，状态更糟。
- **可信度**：**高**。逐段读源码；provider 与并行执行有 README 一手证据。

### τ-bench
- **类型**：论文 + GitHub 项目
- **年份**：2024（arXiv）/ 2025（会议）
- **会议或来源**：ICLR 2025（poster）— 依 `venue-verified.md`（OpenReview venue）。⚠️ **本会话独立复核时，OpenReview `notes/search` 对 τ-bench 未命中，且官方 README 的 BibTeX 是 `@misc{yao2024tau, ... eprint={2406.12045}}`（纯 arXiv 条目）**，与"ICLR 2025"存在口径差异，建议第 6 节保留双记。
- **官方链接**：[arXiv 2406.12045](https://arxiv.org/abs/2406.12045)｜[sierra-research/tau-bench](https://github.com/sierra-research/tau-bench)
- **主要解决的问题**：评测"工具调用 + 与模拟用户多轮对话 + 遵守领域规则"的复合能力，并提出可靠性指标 `pass^k`。
- **核心机制**：`Env` 持有一个内存数据库 `self.data`（dict），工具是 `Tool` 子类的 `invoke(data=..., **kwargs)` 静态方法；`User` 是 LLM 模拟器；奖励 = 终局 DB 状态与"GT 动作重放后状态"的哈希相等。
- **关键数据结构**：`self.data`（业务库）、`self.actions: List[Action]`、`Task.actions`（GT 动作序列）、`Task.outputs`（必须说出的内容）、`consistent_hash`/`to_hashable`、`RewardResult{RewardActionInfo | RewardOutputInfo}`。
- **执行流程**：`reset(task_index)` → `self.data = self.data_load_func()`（重新载入）+ `user.reset(instruction)`；`step(action)`：`RESPOND` → 交给 user，`"###STOP###" in observation` 即 done；工具名 → `tools_map[name].invoke(data=self.data, **kwargs)`；`done` 时 `calculate_reward()`。
- **如何处理失败**：`except Exception as e: observation = f"Error: {e}"`——**异常被字符串化后当作正常观测回喂**。没有错误类型、没有重试、没有幂等键。这是 18 个工作里最"朴素"的失败处理，却也是主流 agent 框架的实际做法。
- **如何处理状态变化**：`calculate_reward()` 的写法值得单独说：先算当前 `data_hash`，然后 `self.data = self.data_load_func()`（重置到初态），**把 GT 动作逐条 `self.step(action)` 重放一遍**，再算 `gt_data_hash`，最后 `r_actions = data_hash == gt_data_hash`。这是**用确定性重放构造期望状态**，一种标准的对账（reconciliation）手法。
- **如何处理记忆**：不处理。context 由 agent 侧自行组装；env 只回观测。
- **如何评测**：DB 全量哈希相等（`r_actions`）+ 必需输出串包含（`r_outputs`，大小写与逗号归一）；`pass^k` 定义（论文 HTML 原文）："the chance that all k i.i.d. task trials are successful, averaged across tasks"，与 `pass@k`（至少一次成功）相对；论文报告 "pass^8 < 25% in retail"。
- **真实证据**：【工程实现】`tau_bench/envs/base.py`（`reset` / `step` 的 `except` / `calculate_reward` 的重放对账 / `get_data_hash`）、`tau_bench/envs/tool.py`（`Tool.invoke` 抽象）。**【论文机制】**`pass^k` 定义与 retail `pass^8 < 25%` 来自 arXiv HTML 正文抓取。
- **对 Atlas 的可借鉴点**：**(1) 哈希相等判分**是"状态一致性检查"最便宜的实现，Atlas 的 audit 可以用"关键实体集合的规范化哈希"替代自由文本判断。**(2) GT 动作重放**= 用期望轨迹重建期望状态，再对账——这直接就是 `PendingToolCall` 对账器的原型。**(3) `pass^k` 的可靠性视角**：Atlas 的自适应/恢复能力必须用"k 次全成功"衡量，单次成功率会掩盖不确定性处理的价值。
- **不适合 Atlas 的地方**：单进程内存 DB，没有网络、没有部分成功、没有并发；异常字符串化使"未知提交"不可表达。作为基准它足够，作为架构参考它太轻。
- **可信度**：**高**（代码与论文文本双验）。venue 有争议，已在第 6 节标注。

### AgentBench
- **类型**：论文 + GitHub 项目
- **年份**：2023（arXiv）/ 2024（会议）
- **会议或来源**：ICLR 2024（poster）— `venue-verified.md`（arXiv Comments + OpenReview 双证）
- **官方链接**：[arXiv 2308.03688](https://arxiv.org/abs/2308.03688)｜[THUDM/AgentBench](https://github.com/THUDM/AgentBench)
- **主要解决的问题**：用 8 个异构交互环境横向比较 LLM 作为 agent 的能力。
- **核心机制**：**client/server 拆分**。服务端 = controller + 多个 task worker（每类任务一个 docker 服务），用 **Redis 做容器分配**；客户端 `TaskClient` 只负责"拿历史 → 问模型 → 提交动作"的循环。
- **关键数据结构**：`StartSampleRequest{name, index}`、`session_id`、`InteractRequest{session_id, agent_response}`、`SampleStatus.RUNNING`、`TaskClientOutput{error, info, output}`、`TaskError` 枚举。
- **执行流程**：`POST /start_sample` → 拿 `sid` + 首轮 output → `while status == RUNNING`：`agent.inference(result["output"]["history"])` → `POST /interact` → 更新 `latest_result`。
- **如何处理失败**：**唯一有分类的系统**——`TaskError = {START_FAILED, INTERACT_FAILED, AGENT_FAILED, NETWORK_ERROR, NOT_AVAILABLE}`。分支行为：`/start_sample` 406 → `NOT_AVAILABLE`；`/start_sample` 非 200 → `START_FAILED`；agent 推理异常 → `POST /cancel` 后 `AGENT_FAILED`；`/interact` 非 200 → `/cancel` 后 `INTERACT_FAILED`；**`/interact` 网络异常 → 直接返回 `NETWORK_ERROR`，不 `/cancel`、不重试、不查询**。
- **如何处理状态变化**：状态全在服务端（`session_id` 绑定），每轮把**全量 history** 回传，客户端不缓存、不比对版本。所以"状态变化"表现为 history 变长，而不是状态 diff。
- **如何处理记忆**：客户端无记忆；服务端 history 是唯一记忆。
- **如何评测**：`calculate_overall()` 汇总 `SampleStatus` 计数；各任务自带 metric；`get_concurrency()` 通过 worker 的 `capacity - current` 反推可并行度。
- **真实证据**：【工程实现】`src/client/task.py:10-16`（`TaskError` 五类）、`run_sample()`（`AGENT_FAILED`/`INTERACT_FAILED` 分支调 `/cancel`，`NETWORK_ERROR` 分支不调）、`README.md`（docker compose 起 `alfworld`/`dbbench`/`knowledgegraph`/`os_interaction`/`webshop` worker + "Redis server (for container allocation)"）。
- **对 Atlas 的可借鉴点**：**(1) `session_id` + 服务端持有状态**的架构，天然适合 Atlas 的"工具在远端、状态在远端"场景；`session_id` 就是天然的关联键。**(2) `get_concurrency()` 用 `capacity-current` 做背压**，Atlas 的并发调度可抄。**(3) `TaskError` 分类**作为起点，但必须补第六类 `UNKNOWN_COMMIT`——这正是它的漏洞所在。
- **不适合 Atlas 的地方**：`NETWORK_ERROR` 分支的漏洞（不 cancel / 不查询 / 不重试）会在服务端留下悬挂 session：容器被占用直到超时，且该 sample 的结果永远不知真假。**这是"不处理未知提交"的教科书式代价。**另外 README 自己承认 "the current implementation of `alfworld` leaks memory and disk space until the task worker is restarted"——工程健壮性不足。
- **可信度**：**高**（客户端代码逐行读；服务端结构由 README 与 client 协议反推，未读服务端源码 → 服务端内部实现标 **待核实**）。

### SWE-bench
- **类型**：论文 + GitHub 项目
- **年份**：2023（arXiv）/ 2024（会议）
- **会议或来源**：ICLR 2024（oral）— 本会话 OpenReview 命中 venue = "ICLR 2024 oral"
- **官方链接**：[arXiv 2310.06770](https://arxiv.org/abs/2310.06770)｜[SWE-bench/SWE-bench](https://github.com/SWE-bench/SWE-bench)（原 `princeton-nlp/SWE-bench` 301 重定向至此）
- **主要解决的问题**：用 2294 个真实 GitHub issue + 对应 PR 评测"改代码库解决 issue"。
- **核心机制**：**每实例一个 docker 容器**，容器内 checkout 到 base commit，应用模型 patch，再应用 test patch，跑 `FAIL_TO_PASS` 与 `PASS_TO_PASS` 两组用例。容器是一次性的。
- **关键数据结构**：`TestSpec{instance_id, FAIL_TO_PASS, PASS_TO_PASS}`、`run_id`、容器名 `sweb.eval.<instance_id>.<run_id>`、eval 报告 `{FAIL_TO_PASS:{success,failure}, PASS_TO_PASS:{...}}`。
- **执行流程**：`create_container()`（含 ghost container 清理）→ `copy_to_container` 模型 patch / eval script → 运行 → `get_logs_eval()` 解析日志 → `get_eval_report()` → `get_resolution_status()`。
- **如何处理失败**：三种确定状态：`APPLY_PATCH_FAIL`（patch 都打不上）、测试失败（F2P 未过）、回归（P2P 掉了）。`create_container()` 里有 "ghost container" 处理：`docker.errors.APIError` 时给容器名加时间戳后缀重试。都是**确定失败**，没有"未知"。
- **如何处理状态变化**：用"重建容器"取代"恢复状态"。每实例从镜像重新起容器，所以不存在跨任务状态污染——**代价是极慢**。
- **如何处理记忆**：无。
- **如何评测**：`resolved` = 全部 F2P 通过 **且** 全部 P2P 保持；`compute_fail_to_pass` / `compute_pass_to_pass` 给比例。这是"状态 diff 式"判分：不是看容器最终内容，而是看测试前后状态转移是否如预期。
- **真实证据**：【工程实现】`swebench/harness/run_evaluation.py`（`create_container`、ghost container、`copy_to_container`）、`swebench/harness/grading.py`（`get_logs_eval`、`get_eval_report`、`compute_fail_to_pass`、`get_resolution_status`）、`swebench/harness/docker_utils.py`（`cleanup_container`）。
- **对 Atlas 的可借鉴点**：**(1) "用重建代替恢复"** 是最强的隔离手段，Atlas 可以在高风险工具上采用"每次重建工作区"而非"回滚工作区"。**(2) F2P/P2P 双集**是"改动没有副作用"的判据，等价于 Atlas 的"修复目标 + 不回归"双约束。**(3) ghost container 清理**提醒：僵尸资源必须显式清理，Atlas 的 checkpoint 恢复也要清理"上次未完成的容器/临时目录"。
- **不适合 Atlas 的地方**：单实例分钟级成本，与长程任务的交互频率不相容；无 in-doubt 处理（容器崩了就重跑，反正在容器外没有外部副作用）。
- **可信度**：**高**。

### SWE-agent
- **类型**：论文 + GitHub 项目
- **年份**：2024
- **会议或来源**：NeurIPS 2024（poster）— OpenReview venue = "NeurIPS 2024 poster"
- **官方链接**：[arXiv 2405.15793](https://arxiv.org/abs/2405.15793)｜[SWE-agent/SWE-agent](https://github.com/SWE-agent/SWE-agent)
- **主要解决的问题**：证明 **agent-computer interface（ACI）** 的设计（而非只换模型）能显著提升代码任务成功率。
- **核心机制**：`SWEEnv` 包住一个 SWE-ReX deployment/runtime（容器或远端），把"发一条 bash 命令 + 拿回输出"封装成 `communicate()`；文件读写走 `read_file`/`write_file`；`EnvHook` 做生命周期钩子。
- **关键数据结构**：`EnvironmentConfig{post_startup_commands, post_startup_command_timeout}`、`self.repo`（含 `get_reset_commands()`）、`BashAction(command, timeout, check)`、`EnvHook` 抽象。
- **执行流程**：`start()` = `reset()` + 跑 `post_startup_commands`；`reset()` = `cd /` → `_copy_repo()` → `_reset_repository()`（`git` checkout 到 base commit）→ `_chook.on_environment_startup()`；每步 `communicate(command, timeout=..., check=...)`。
- **如何处理失败**：`check` 三档：`ignore`（不取 exit code，代码注释说 "more stable"）、`warn`（非零只 log）、`raise`（非零则 **`self.close()` 先销毁 deployment，再 `raise RuntimeError`**）。给了 `interrupt_session()` 供中断。**失败即销毁环境**——这等价于"放弃并重建"，不试图对齐。
- **如何处理状态变化**：`hard_reset()` = `close()` + `start()`（彻底重来）；`reset()` 是轻量版（git 回滚 + 复用容器）。**没有环境状态模型**，容器文件系统就是状态。
- **如何处理记忆**：无。历史由 agent 端 prompt 维护。
- **如何评测**：在 SWE-bench / HumanEvalFix 上跑 pass@1（论文报告 12.5% / 87.7%）。
- **真实证据**：【工程实现】`sweagent/environment/swe_env.py`：`hard_reset`/`reset`/`_reset_repository`、`communicate()` 的 `check` 三档与 `self.close(); raise RuntimeError(msg)`。
- **对 Atlas 的可借鉴点**：**(1) `check` 三档是"失败严格度"的可配置化**——Atlas 的工具封装也应该让调用方声明"这条命令失败要紧不要紧"，而不是全局一刀切。**(2) 失败即销毁**（`close()` 再抛）是"避免在坏状态上继续"的极端但正确的选择。
- **不适合 Atlas 的地方**：销毁环境会丢失全部上下文，对长程任务不可接受；异常无分类（exit code 非零一律同质）；无 in-doubt。
- **可信度**：**高**。

### OpenHands
- **类型**：论文 + GitHub 项目
- **年份**：2024（arXiv）/ 2025（会议）
- **会议或来源**：ICLR 2025（poster）— **本会话 OpenReview 独立命中** venue = "ICLR 2025 Poster"，该条不在 `venue-verified.md` 中，属本次新增核实项
- **官方链接**：[arXiv 2407.16741](https://arxiv.org/abs/2407.16741)｜[OpenHands/OpenHands](https://github.com/OpenHands/OpenHands)。**注意**：`main` 分支已是前端 monorepo（`src/` 为 React 组件，共 3132 个文件），论文对应的 Python 实现需按 tag 取，本报告证据取自 `1.0.0` tag。
- **主要解决的问题**：给 AI 软件开发者一个可扩展的通用 agent 平台：沙箱执行、多 agent 协作、评测接入。
- **核心机制**：**事件流（event stream）作为唯一真源**。所有动作与观测都是 `Event`，写入 `EventStream`（同时落 FileStore），订阅者（agent / runtime / 前端）从流里消费；`State` 记录 `start_id`/`end_id` 界定"本 agent 的历史区间"。
- **关键数据结构**：`EventStream`（`_subscribers: dict[str, dict[str, Callable]]`、`cur_id`、`_write_page_cache`、`secrets`）、`State`（`session_id`, `agent_state`, `start_id`, `end_id`, `iteration_flag`, `budget_flag`, `delegate_level`, `history`, `last_error`, `extra_data`）、`AgentController._pending_action_info`、`StuckDetector`。
- **执行流程**：`add_event(event, source)` → 校验 `event.id == Event.INVALID_ID`（防重入）→ `_timestamp`/`_source` → 加锁分配 `event._id = cur_id; cur_id += 1` → 追加到写缓存页 → `event_to_dict` + `_replace_secrets` → 单事件 JSON 写 FileStore → 整页满 `cache_size` 时写页缓存 → `self._queue.put(event)` 异步分发。`_step()` → agent 出 action → `_pending_action` 置位 → runtime 执行 → observation 事件回流 → `_pending_action` 清空。
- **如何处理失败**：`_react_to_exception` → `set_agent_state_to(AgentState.ERROR)`；`RATE_LIMITED` 单独状态；`StuckDetector` 检测循环（`loop_start_idx`、`loop_type`），`attempt_loop_recovery()` 暂停并给 `/resume 1|2` 选项。**pending action 超 60s 只 log `PENDING_ACTION_TIMEOUT`，不清除、不放弃**（源码注释 "just for logging purposes"、"don't clear it"）。
- **如何处理状态变化**：`SearchEvents` 倒序 + `_is_awaiting_observation()` 用"最近一个 `AgentStateChangedObservation` 是不是 RUNNING"反推是否在等观测；resume 时 `state_tracker._init_history(self.event_stream)` 从流重建历史（注释 "Always load from the event stream to avoid losing history"）。
- **如何处理记忆**：`State.history` + 事件流 + delegate 的父子 interval（`start_id`/`end_id`）；有 condenser（论文提到上下文压缩），本报告未展开。
- **如何评测**：接入 SWE-bench、WebArena 等外部基准；论文报告 15 个挑战性任务。
- **真实证据**：【工程实现】`openhands/events/stream.py`（`add_event` 的全序 id、写前落盘、页缓存顺序、`_replace_secrets`、`Event.INVALID_ID` 防重入）、`openhands/controller/agent_controller.py:112` 与 `_pending_action` property/setter（60s 仅 log）、`openhands/controller/state/state.py`（`State` 字段全表，**无 pending**）、`agent_controller.py::_is_awaiting_observation`。
- **对 Atlas 的可借鉴点**：**(1) 全序单调 `cur_id` 是事件溯源的核心**，Atlas 的 event log 若没有全局单调序，replay 无法定序。**(2) "先写单事件、再写页缓存"的顺序设计**，作者注释 "if it is not present during reads then it will simply be bypassed"——**崩溃一致性靠写入顺序保证**，Atlas 的 checkpoint 落盘顺序应当照抄这个思路。**(3) `Event.INVALID_ID` 检查**是事件级去重的最小实现。**(4) `_replace_secrets` 在持久化之前做**，说明"落盘前脱敏"应该内建在写入路径里，而非事后清洗。
- **不适合 Atlas 的地方**：**pending action 不持久化 + 超时只 log**，这正是 Atlas 要解决的缺口；`_is_awaiting_observation()` 用日志反推状态，等于承认"没有状态机"。
- **可信度**：**高**（三个文件逐段读）。**限制**：`main` 分支结构已变，本报告结论绑定 `1.0.0` tag；更高版本行为**待核实**。

### Voyager
- **类型**：论文 + GitHub 项目
- **年份**：2023
- **会议或来源**：TMLR（2024 接收）— `venue-verified.md`，OpenReview venue = "Accepted by TMLR"
- **官方链接**：[arXiv 2305.16291](https://arxiv.org/abs/2305.16291)｜[MineDojo/Voyager](https://github.com/MineDojo/Voyager)
- **主要解决的问题**：Minecraft 里的开放式终身学习：自动课程 + 技能库 + 迭代提示。
- **核心机制**：三个组件——自动课程（curriculum agent）、可执行代码技能库（skill manager）、带环境反馈的迭代提示（action agent + critic agent）。
- **关键数据结构**：`events`（`(event_type, event)` 列表，末项必须是 `"observe"`）、结构化 status（biome / timeOfDay / health / food / position / equipment / inventoryUsed / inventory / voxels）、`skill_library`（代码 + 描述向量检索）。
- **执行流程**：`bridge.reset(mode="hard"|"soft")` → 循环：课程出任务 → 代码生成 → `bridge.step()` 执行 → 收集 events → critic 判定成败 → 成功则把代码存入技能库。
- **如何处理失败**：`CriticAgent.render_human_message()` 遍历 `events`，**遇到任何 `onError` 事件直接 `return None`**，即"这次尝试作废"。之后由上层重试（新尝试、新代码），失败信息不写回任何持久状态。
- **如何处理状态变化**：`bridge` 里 `has_reset` 守卫（未 reset 就 step 会 `RuntimeError("Environment has not been reset yet")`）；首次 reset 用 `hard`（新世界），之后统一 "All the reset in step will be soft"；提供 `pause()`/`unpause()`。**没有状态模型**，每次从 events 末项重读结构化观测。
- **如何处理记忆**：**技能库是唯一长期记忆**（成功代码），失败经验不沉淀。
- **如何评测**：Minecraft 科技树解锁数、独特物品数、探索距离；critic 的 LLM 判定作为任务内成功信号。
- **真实证据**：【工程实现】`voyager/agents/critic.py`（`events` 遍历 + `onError` → `return None`；`events[-1][0] == "observe"` 断言；结构化 status 抽取）、`voyager/env/bridge.py`（`has_reset` 守卫、`reset` 的 hard/soft、`pause`/`unpause`）。README 描述三组件与技能库复用。
- **对 Atlas 的可借鉴点**：**(1) "失败即作废本次尝试，但把成功固化为可复用资产"**——Atlas 可以对应为"失败不写长期记忆，成功写入可复用子计划"。**(2) 技能库 = 程序的记忆**，比文本记忆更抗漂移；Atlas 的 memory 图若能存"可执行子流程"，就能避免重复探索。**(3) `pause/unpause` 在环境层提供**，说明暂停不仅要停 agent，还要停环境——Atlas 的 pause 应明确"冻结环境交互通道"。
- **不适合 Atlas 的地方**：critic 判定是 LLM 主观判断，不可审计、不可重放；失败不留痕导致无法做"过期信息检测"；状态读取是"末项快照"而非版本化。
- **可信度**：**高**（critic/bridge 源码）；论文层面的三组件描述取自 README 与摘要，**未逐节精读原文**。

下表六个工作是"工具调用 / 规划 / 反馈 / 状态"四条线的思想来源。它们**都没有环境状态模型**，价值在于提供词汇与反例；故合并为紧凑 15 字段格式。

### ReAct
- **类型**：论文｜**年份**：2022/2023｜**会议或来源**：ICLR 2023
- **官方链接**：[arXiv 2210.03629](https://arxiv.org/abs/2210.03629)
- **主要解决的问题**：让推理与行动在同一轨迹里交替，用外部观测纠正推理。
- **核心机制**：Thought → Action → Observation 循环，观测回填上下文再继续推理。
- **关键数据结构**：thought/action/observation 三元组轨迹、prompt 模板。
- **执行流程**：模型出 Thought+Action → 环境执行 → Observation → 拼回 prompt。
- **如何处理失败**：**不处理**，错误文本只是文本。
- **如何处理状态变化**：不处理，隐含假设"每次观测都是最新的"。
- **如何处理记忆**：完整轨迹即记忆（受上下文长度限制）。
- **如何评测**：HotpotQA / FEVER / ALFWorld / WebShop 成功率。
- **真实证据**：【论文机制】arXiv abs 页 title/comments/subjects（本会话抓取）；机制为通行理解。
- **对 Atlas 的可借鉴点**：观测回填是"执行后重新感知"的最小雏形；`PendingToolCall` 解析出的观测应作为一等事件回填，而非当字符串塞 prompt。
- **不适合 Atlas 的地方**：无状态概念，无法区分"动作失败"与"成功但观测丢失"。
- **可信度**：**中**（元数据高；机制未逐节验证）。

### Toolformer
- **类型**：论文｜**年份**：2023｜**会议或来源**：NeurIPS 2023 oral
- **官方链接**：[arXiv 2302.04761](https://arxiv.org/abs/2302.04761)
- **主要解决的问题**：让 LM 自监督学会"何时调哪个 API、传什么参、怎么用结果"。
- **核心机制**：自监督构造调用候选 → 按"调用后 loss 是否下降"筛选 → 微调模型在文本中插 API 调用。
- **关键数据结构**：带 `<API>` 标记的 token 序列、每 API 少量示例。
- **执行流程**：离线采样→执行→保留有增益者→训练；推理时按 token 触发→结果拼回。
- **如何处理失败**：不处理，失败调用只是"没增益"被过滤。
- **如何处理状态变化**：不处理，**无状态、无会话**。
- **如何处理记忆**：参数化（微调进权重）。
- **如何评测**：零样本下游任务（LAMA/数学/QA）与语言建模 loss。
- **真实证据**：【论文机制】OpenAlex 摘要原文 "decide which APIs to call, when to call them, what arguments to pass"。
- **对 Atlas 的可借鉴点**：工具调用要有稳定语法位置（结构化 action），否则事件流里无法可靠解析与对账。
- **不适合 Atlas 的地方**：离线训练范式；无副作用概念。
- **可信度**：**中高**（摘要级）。

### LATS
- **类型**：论文｜**年份**：2023/2024｜**会议或来源**：ICML 2024
- **官方链接**：[arXiv 2310.04406](https://arxiv.org/abs/2310.04406)
- **主要解决的问题**：MCTS 引入语言 agent，统一推理、行动与规划，用环境反馈做搜索。
- **核心机制**：MCTS + LM value function + self-reflection；节点是状态、边是动作，回传用环境奖励与 LM 估值。
- **关键数据结构**：搜索树（节点含 value 与 reflection）、trajectory。
- **执行流程**：选择→扩展→执行得观测→估值→回传→迭代。
- **如何处理失败**：失败作为低奖励回传靠搜索绕开；**没有"未知"档**，观测缺失被当普通观测。
- **如何处理状态变化**：不建模；节点隐含环境状态但**无版本/哈希**，无法判断两节点是否同一状态（重访不可识别）。
- **如何处理记忆**：树 + 反思文本。
- **如何评测**：HumanEval（pass@1 92.7%）、WebShop、HotpotQA。
- **真实证据**：【论文机制】OpenAlex 摘要原文 "integrate Monte Carlo Tree Search into LATS ... along with LM-powered value functions and self-reflections"、"incorporation of an environment for external feedback"。
- **对 Atlas 的可借鉴点**：搜索需要**状态同一性**——分支探测必须给环境状态一个可比较指纹（可扩展 `state_fingerprint`）。
- **不适合 Atlas 的地方**：MCTS 假设可重复访问同一状态，对真实副作用（发邮件/下单）不成立；这正是不该照搬搜索的原因。
- **可信度**：**中高**（摘要级）。

### Reflexion
- **类型**：论文｜**年份**：2023｜**会议或来源**：NeurIPS 2023 poster
- **官方链接**：[arXiv 2303.11366](https://arxiv.org/abs/2303.11366)
- **主要解决的问题**：不更新权重，用语言反馈强化 agent 的 trial-and-error 学习。
- **核心机制**：对反馈信号做语言反思，存进 **episodic memory buffer**，下次试验注入上下文。
- **关键数据结构**：反思文本列表、反馈信号（标量或自由文本，外部或内部模拟）。
- **执行流程**：执行→得反馈→生成反思→存 buffer→下一 trial 带 buffer 重试。
- **如何处理失败**：失败→反思→重试；**转成文本经验**，不区分失败类型。
- **如何处理状态变化**：不处理；反思是"上次为何失败"，不是"世界现在如何"。
- **如何处理记忆**：episodic（跨 trial），本文核心贡献。
- **如何评测**：HumanEval（91% pass@1）、ALFWorld、HotpotQA。
- **真实证据**：【论文机制】OpenAlex 摘要原文 "verbally reflect on task feedback signals, then maintain their own reflective text in an episodic memory buffer"。
- **对 Atlas 的可借鉴点**：反思是低成本经验压缩，可作 `repairing` 输入；但须与环境状态分离。
- **不适合 Atlas 的地方**：**反思不带时间戳与失效条件**，天然过期；引入必须绑 `observed_at` 与失效事件。
- **可信度**：**高**（摘要原文）。

### ReWOO
- **类型**：论文｜**年份**：2023｜**会议或来源**：arXiv preprint
- **官方链接**：[arXiv 2305.18323](https://arxiv.org/abs/2305.18323)
- **主要解决的问题**：把推理与观测解耦，降 token 消耗与重复执行。
- **核心机制**：Planner 一次生成含变量占位（如 `#E1`）的完整计划 → Worker 顺序执行填充 → Solver 汇总。
- **关键数据结构**：plan（步骤 + 占位符依赖）、evidence 映射（变量→工具输出）。
- **执行流程**：plan → 逐步执行 → 填变量 → solve。
- **如何处理失败**：摘要称 "**demonstrates robustness under tool-failure scenarios**"，表现为占位符取不到值时 Solver 仍能基于已有证据作答（降级）；但**无重试、无对账、无部分成功语义**。
- **如何处理状态变化**：最激进的"去观测"：计划执行前固定，执行期不重规划。
- **如何处理记忆**：plan + evidence 即工作记忆。
- **如何评测**：六个公开 NLP 基准 + 自建数据集；HotpotQA 上 5× token 效率、+4% 准确率。
- **真实证据**：【论文机制】OpenAlex 摘要原文 "detaches the reasoning process from external observations"。
- **对 Atlas 的可借鉴点**：**"变量占位 + 证据填充"是 PendingToolCall 的天然载体**——未填充=未决，有值=已决。
- **不适合 Atlas 的地方**：计划不可变，无法重规划；对失败"鲁棒"是忽略缺失证据而非修复。
- **可信度**：**中高**（摘要级；鲁棒性的具体实现未核）。

### CoALA
- **类型**：论文｜**年份**：2023/2024｜**会议或来源**：TMLR
- **官方链接**：[arXiv 2309.02427](https://arxiv.org/abs/2309.02427)
- **主要解决的问题**：给语言 agent 一个系统性认知架构框架（记忆模块 + 结构化动作空间 + 决策过程）。
- **核心机制**：工作/情景/语义/程序四类记忆模块；动作分内部（记忆读写、推理）与外部（环境交互）；决策 = 规划→执行→评估。
- **关键数据结构**：四类记忆模块、动作空间分类、决策循环。
- **执行流程**：概念框架，非具体实现。
- **如何处理失败**：把"评估"作为循环一环，但**不规定失败分类与恢复策略**。
- **如何处理状态变化**：**把世界状态放进工作记忆**，未规定如何与真实环境对账——正是 Atlas 可填的空。
- **如何处理记忆**：四类记忆划分是主要贡献。
- **如何评测**：综述性论文，用框架回溯组织已有工作，无自建实验。
- **真实证据**：【论文机制】OpenAlex 摘要原文 "modular memory components, a structured action space to interact with internal memory and external environments, and a generalized decision-making process"。
- **对 Atlas 的可借鉴点**：**"内部动作 vs 外部动作"二分**对应"纯计算工具 vs 有副作用工具"，是 PendingToolCall 只施加于外部动作的理论依据。
- **不适合 Atlas 的地方**：无实现、无失败语义、无对账；只能当词汇表。
- **可信度**：**中高**（摘要级，框架性论文）。

### WorkArena
- **类型**：论文 + GitHub 项目
- **年份**：2024
- **会议或来源**：ICML 2024（poster）— **本会话 OpenReview 独立命中** venue = "ICML 2024 Poster"（forum `BRfqYrikdo`）。注：OpenReview 另有《WorkArena++》NeurIPS 2024 D&B poster，与 WorkArena 本体是两篇，勿混
- **官方链接**：[ServiceNow/WorkArena](https://github.com/ServiceNow/WorkArena)（仓库实测存在）｜OpenReview `https://openreview.net/forum?id=BRfqYrikdo`
- **主要解决的问题**：在真实企业软件（ServiceNow）上评测 web agent 完成"知识工作"的能力。
- **核心机制**：基于 ServiceNow 实例的多任务套件，与 BrowserGym 接口对齐（可被同一 agent 批量跑）。
- **关键数据结构**：任务定义 + validator（复用 BrowserGym 的 `validate` 协议）。
- **执行流程**：依赖一个已实例化的 ServiceNow 环境，任务通过 BrowseComp/WorkArena 的 `AbstractBrowserTask` 子类定义。
- **如何处理失败**：继承 BrowserGym 的 `last_action_error` 字符串化路径。
- **如何处理状态变化**：企业软件是共享可变状态（记录会被改），但**没有版本化读取**，agent 无法知道读到的记录是否刚被别人改过。
- **如何处理记忆**：无。
- **如何评测**：任务 validator 每步/终局判定；与 BrowserGym 一致。
- **真实证据**：【工程实现】仓库存在性（GH API 200，default_branch=main）；**【论文机制】**venue 由 OpenReview 字段核实。具体 validator 实现**未逐个读**（本次时间预算内未抓取），标 **待核实**。
- **对 Atlas 的可借鉴点**：**共享可变企业状态**是真实场景，Atlas 需要"读到的记录带版本号/更新时间"，否则必然按过期信息行动。
- **不适合 Atlas 的地方**：依赖重量级商业实例，不适合本地回归。
- **可信度**：**中**。venue 与仓库高可信；实现细节未验证。

### TheAgentCompany
- **类型**：论文 + GitHub 项目
- **年份**：2024（arXiv）/ 2025
- **会议或来源**：⚠️ **口径冲突**。`venue-verified.md` 记为 "arXiv preprint"（依 arXiv Comments "Preprint"）；但本会话另获两条相反证据：OpenReview venue = "NeurIPS 2025 Datasets and Benchmarks Track poster"、OpenAlex 该文 DOI `10.52202/085713-0315` 的 venue 为 "neural information processing systems"、type=conference-paper。**建议按 NeurIPS 2025 D&B 采信，但保留待统一标注。**
- **官方链接**：[arXiv 2412.14161](https://arxiv.org/abs/2412.14161)｜[TheAgentCompany/TheAgentCompany](https://github.com/TheAgentCompany/TheAgentCompany)
- **主要解决的问题**：让 agent 像"数字员工"一样在模拟小公司里浏览内部网站、写代码、跑程序、和同事沟通，评测长程工作任务的自主完成率。
- **核心机制**：自包含环境（内部网站 + 数据 + NPC 同事）；**每个任务是一个 docker 镜像**，内含 `/utils/init.sh`（初始化）、`/utils/eval.py`（判分入口）、`/instruction/task.md`（任务书）、`/workspace`。
- **关键数据结构**：任务镜像目录结构、`checkpoints`/`subcheckpoints`（子检查点给部分分）、evaluator（确定性 + LLM-based 两类）。
- **执行流程**：拉起任务容器 → 跑 `init.sh` → agent 在容器内工作 → `eval.py` 判分并汇总 subcheckpoints。
- **如何处理失败**：结果判定为主（primary）、子检查点为辅（secondary）；失败无恢复语义。
- **如何处理状态变化**：**有真实的状态扰动源**——NPC 同事会发消息、改文档（README 列 "Multiple Agent Interaction"、镜像含 `/utils/npc`），但**没有"这份信息是否仍有效"的机制**。
- **如何处理记忆**：无。
- **如何评测**：175 个任务镜像；"Result-based evaluation (primary)" + "Subcheckpoints checking (secondary)"；"Deterministic evaluators" + "LLM-based evaluators"；README 称 "Quick system reset in minutes when needed"。
- **真实证据**：【工程实现】`README.md` 的镜像结构树、`docker run` 用法、"175 task images"、"Subcheckpoints checking (secondary)"、"Quick system reset in minutes"。**【论文机制】**OpenAlex 摘要："self-contained environment with internal web sites and data that mimics a small software company"、"the most competitive agent can complete 30% of tasks autonomously"。
- **对 Atlas 的可借鉴点**：**(1) 子检查点 = 部分成功的显式表达**，正是 Atlas `PendingToolCall` 里 `PARTIAL` 语义的评测侧对应物。**(2) `init.sh` / `eval.py` 的镜像契约**：把"准备状态"与"判定状态"写成两个可执行入口，Atlas 的 checkpoint 也应把"准备"与"校验"分离成可重放的两个函数。
- **不适合 Atlas 的地方**：容器级隔离太重；无 in-doubt。
- **可信度**：**中高**（README 一手 + OpenAlex 摘要）。venue 冲突已标。

### AppWorld
- **类型**：论文 + GitHub 项目
- **年份**：2024
- **会议或来源**：ACL 2024 — `venue-verified.md`（arXiv Comments "ACL'24 Camera Ready"）；本会话 OpenReview 亦命中 venue = "ACL 2024"
- **官方链接**：[arXiv 2407.18901](https://arxiv.org/abs/2407.18901)｜[StonyBrookNLP/appworld](https://github.com/StonyBrookNLP/appworld)
- **主要解决的问题**：给"交互式编程 agent"一个可控世界：9 个日常 app、457 个 API、约 100 个虚构用户，任务需要写带控制流的代码而非简单 API 序列。
- **核心机制**：`AppWorld` 环境对象守住一个任务世界（每个任务一套 DB），提供 `execute(code)` 执行 agent 写的 python 代码，`evaluate()` 用 **state-based unit tests** 判分，并检查 **collateral damage**（非预期改动）。
- **关键数据结构**：`environment_io`（每次交互的 `{number, input, output}`）、`data/tasks/<task_id>/dbs`、`api_calls.jsonl`（`request_tracker`）、`TestTracker`、`max_interactions`、`safety_guard`。
- **执行流程**：初始化（任务 DB 载入）→ `execute(code)`：安全检查（`is_syntax_safe`）→ `ast.parse` → 注入 `max_interactions` 熔断 → 执行 → 记录 `environment_io` + `save_logs()` → `evaluate()` 取 `TestTracker`。
- **如何处理失败**：多层确定化处理：语法安全 → `SyntaxError` 转 message 返回；`sqlite3.ProgrammingError|Cannot operate on a closed database` 被特判成 `raise Exception("Looks like you are operating on a closed task world object.")`（**一种"句柄过期"检测**）；超预算时把代码改成 `raise Exception(f"Maximum number of executions ({n}) reached.")`。所有错误都以 message 形式回给 agent，**无重试、无 in-doubt**。
- **如何处理状态变化**：DB 是唯一状态；每次交互全量落盘 `environment_io.md` + `api_calls.jsonl`，构成完整可审计轨迹（这一点的成熟度高于其它基准）。**但没有"状态版本"**，agent 无法判断两次读之间世界是否变了。
- **如何处理记忆**：无（日志是审计用，不是 agent 记忆）。
- **如何评测**：`evaluate_task()` + `task.ground_truth` 的 state-based 单测；支持"多种完成方式"+"检查 unexpected changes"（摘要原文 "allowing for different ways of completing a task while also checking for unexpected changes, i.e., collateral damage"）。
- **真实证据**：【工程实现】`src/appworld/environment.py`：`execute()` 的安全检查/熔断/closed-db 特判/`environment_io` 追加、`evaluate()` → `evaluate_task()`、`_save_api_calls_log`/`_save_environment_io_log`/`save_logs()`。**【论文机制】**摘要的 state-based unit tests + collateral damage。
- **对 Atlas 的可借鉴点**：**"每次交互全量落盘 + 可重放审计"**是最接近 Atlas `event log + replay` 的基准侧实现，可直接对齐事件字段。**collateral damage 检查**是"副作用越界检测"的现成语汇。
- **不适合 Atlas 的地方**：单进程、单机、无并发；`closed database` 特判只是字符串匹配，不是真正的版本/一致性机制。
- **可信度**：**高**（源码逐段 + 摘要 + 双源 venue）。

---

## 3. 外部副作用与未知提交状态

### 3.1 谁处理了"动作已发出、结果未知"？

**结论：学术侧零处理；工程侧只有 Temporal/DBOS 一类 durable execution 系统处理了，且它们的做法是"把不确定性变成可判定事件"。**

逐系统证据（均为本会话代码证据）：

| 系统 | 动作已发出的表达 | 未知结果的处理 | 失败分类 | 幂等键 | 去重 | 对账 |
|---|---|---|---|---|---|---|
| WebArena | 无 | 异常→`info["fail_error"]`，回合继续 | 无 | 无 | 无 | 无 |
| BrowserGym | 无 | 异常→`last_action_error`，TimeoutError 特判 | 仅超时 | 无 | 无 | 无 |
| OSWorld | 无 | `step()` 无 try，异常冒泡中断 | 无 | 无 | 无 | 无（仅 snapshot 复原） |
| τ-bench | 无 | 异常 `str()` 成 observation | 无 | 无 | 无 | **判分时用 GT 重放对账** |
| AgentBench | 有 `session_id` | `NETWORK_ERROR` 直接返回，不 cancel/不查询 | **5 类** | 无 | 无 | **无（漏洞）** |
| SWE-bench | 无 | 确定性失败三态（patch 失败/测试失败/回归） | 3 类 | 无 | 无 | 无（重建容器） |
| SWE-agent | 无 | `check` 三档；`raise` 时先 `close()` 再抛 | 3 档严格度 | 无 | 无 | 无 |
| OpenHands | `_pending_action_info`（内存） | 60s 仅 log；`State` 无 pending 字段 | ERROR/RATE_LIMITED/STUCK | 无 | **事件级（`Event.INVALID_ID`）** | `_is_awaiting_observation()` 日志反推 |
| Voyager | 无 | `onError` → critic 返回 None，本次作废 | 无 | 无 | 无 | 无 |
| AppWorld | 无 | 错误串返回 + closed-db 特判 + `max_interactions` 熔断 | 无（按 message） | 无 | 无 | `api_calls.jsonl` 事后审计 |

**这条表的读法**：所有系统都能表达"成功"和"确定失败"，**没有一个能表达"不知道"**。AgentBench 是唯一拿到 `session_id` 这种"可用于反查的把手"的系统，却在网络异常分支把它丢掉了。

### 3.2 工具调用失败的分类学（本报告的归纳，非引文）

把上表归纳成四类，是 Atlas 可以直接采用的分类骨架：

1. **可重试（retriable）**：确定未执行，或执行是幂等的。例：连接被拒、`START_FAILED`（AgentBench）。
2. **不可重试（terminal）**：确定失败且重试无意义。例：`invalid_request`、`APPLY_PATCH_FAIL`、`SyntaxError`。
3. **部分成功（partial）**：动作序列中一部分已生效。例：OSWorld 的 setup 注释 "Setup may mutate the guest before failing"；SWE-bench 的 patch 应用了一半。→ 只能补偿，不能重试。
4. **未知（in-doubt / unknown commit）**：请求已发出，响应未收到，无法判断是否执行。**上表 10 个系统全部缺失这一档。**

### 3.3 工程实践：可引用的原始来源

以下每条均为本会话实际抓取正文并命中的原文（URL 可直接访问）：

- **Saga** — [Azure: Saga pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/saga)。原文："If a step fails, a series of compensating transactions undoes the changes that the completed steps made."
- **Compensating Transaction** — [Azure: Compensating Transaction](https://learn.microsoft.com/en-us/azure/architecture/patterns/compensating-transaction)。原文："A compensating transaction is application specific. It relies on the application having sufficient information to undo the effects of each step in a failed operation." 及 **"Define the steps in a compensating transaction as idempotent commands so that you can repeat them if the compensating transaction itself fails."** —— **补偿自己也会失败，所以补偿必须幂等**；Atlas 的 `compensate` 必须带 `compensation_idempotency_key`。
- **Transactional outbox** — [microservices.io: Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html)。业务库更新与消息发送不能原子。Atlas 对应：**写前日志（本地事务）与真正发出请求必须分开**。
- **Event sourcing** — [microservices.io: Event sourcing](https://microservices.io/patterns/data/event-sourcing.html)。Atlas 的 `event log + replay` 属此模式；硬约束是事件全序、不可变、可重放。
- **Idempotency-Key（协议层）** — [IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)。原文："The idempotency key MUST be unique and MUST NOT be reused with another request with a different request payload. Uniqueness of the key MUST be defined by the resource owner and MUST be implemented by the clients of the resource." 另有 **`Idempotency Fingerprint`** 与 **`Idempotency Key Validity and Expiry`** 两节——键要能表达"同键不同 payload = 冲突"，且需 TTL。
- **幂等重试（实现层）** — [Stripe: Idempotent requests](https://docs.stripe.com/api/idempotent_requests)。原文："supports idempotency for safely retrying requests without accidentally performing the same operation twice... if a connection error occurs, you can safely repeat the request... **Stripe saves the status code and body of the first request made with a given idempotency key after endpoint execution begins**"。最后半句是关键设计：**服务端在"开始执行后"就存下结果**，因此重放请求能拿到第一次的响应——这正是"未知提交"的服务端解法。
- **Durable execution（工作流）** — [Temporal: Workflow Execution](https://docs.temporal.io/workflow-execution)。原文："A Workflow Execution is durable because it executes a Temporal Workflow Definition ... **effectively once and to completion**"；"During a Replay the Commands that are generated are checked against an existing Event History"；"If a failure occurs, the Workflow Execution picks up where the last recorded event occurred in the Event History."
- **Activity 是副作用的边界** — [Temporal: Activities](https://docs.temporal.io/activities)。原文："Larger pieces of functionality should be broken up into multiple Activities. This makes it easier to do failure recovery, have short timeouts, and be idempotent."；"If an Activity attempt fails, it is automatically retried using its Retry Policy."；Heartbeat "for checkpointing. The last recorded Heartbeat details are made available to the Activity function on the next attempt"。
- **"执行了但不知道"如何判定** — [Temporal: Detecting Activity failures](https://docs.temporal.io/encyclopedia/detecting-activity-failures)。四种机制：`Schedule-To-Start`（排队到开工）、`Start-To-Close`（单次执行上限，**无默认值，两个都不设则 Temporal 拒绝该 Activity**）、`Schedule-To-Close`（总时长）、`Activity Heartbeats`（执行中报活）。**这就是 PendingToolCall 的超时三元组原型**：把"没开工"、"开工了但没结束"、"总超时"分开，才能区分"肯定没执行"与"可能执行了"。
- **Checkpoint/恢复（库级）** — [DBOS: Workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial)。原文："Workflows provide durable execution so you can write programs that are resilient to any failure."；"If a workflow is interrupted for any reason, DBOS **automatically recovers its execution from the last completed step**." 另有 "Exactly-Once Event Processing"（[DBOS 首页](https://docs.dbos.dev/)）。**step 粒度即 checkpoint 粒度**——Atlas 的 checkpoint 目前是任务级，工具调用级的粒度缺失正是缺口。
- **Fencing token（过期持有者不得写）** — [Kleppmann: How to do distributed locking](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html)。原文："you need to include a fencing token with every write request... a fencing token is simply a number that increases every time a client acquires the lock"；示例：Client 1 拿 token 33 后长时间停顿，lease 过期，Client 2 拿 34 写入，存储端拒绝 33 的写。**这是"避免按过期状态继续执行"的最强机制**：不是靠 agent 自觉，而是让下游拒绝陈旧写。
- **Read-your-writes / stale read** — [Azure Cosmos DB: Consistency levels](https://learn.microsoft.com/en-us/azure/cosmos-db/consistency-levels) 原文："In session consistency, within a single client session, reads are guaranteed to honor the **read-your-writes**, and write-follows-reads guarantees."；[TiDB: Usage Scenarios of Stale Read](https://docs.pingcap.com/tidb/stable/stale-read/) 原文："**Stale Read is a mechanism** that TiDB applies to ..."（读历史版本，可指定 `AS OF TIMESTAMP`）。前者是 Atlas 该提供的保证（自己写的东西自己一定读得到），后者是 Atlas 该能检测的现象（读到了旧版本）。

---

## 4. 基准的环境状态设计对比

| 基准 | "环境状态"是什么 | reset 机制 | success 判定 | 状态扰动/干扰 | 并发 |
|---|---|---|---|---|---|
| **WebArena** | 5 个自托管 docker 站点 + 浏览器登录态（`storage_state` json） | **全局**：`docker stop/remove` + `docker run` 重建（README "After evaluating the 812 examples"）；单任务只重建浏览器上下文 | 事后 `StringEvaluator`/`URLEvaluator`/`HTMLContentEvaluator` | 无设计内扰动；但**后端 DB 跨任务共享**，任务间会互相污染（非设计意图的扰动） | 多 browser context，**共享同一后端** → 并发不安全 |
| **BrowserGym** | 上层任务集（WebArena/WorkArena/MiniWoB）的活页面 + `page_history` tab 栈 | 新 page/context；任务集各自的 reset | **每步** `task.validate()`（`post_step`）；`terminate_on_infeasible` | 无 | 框架支持多环境，共享后端时同上 |
| **OSWorld** | 一个 VM 的完整磁盘/内存状态 | **VM snapshot 回滚**到 `init_state`；`is_environment_used` 脏标记避免无谓回滚；setup 失败重试 `MAX_RETRIES` | 事后 `evaluate()`：`result_getter` 取数 / `infeasible` 要求最后动作是 FAIL | 无（但有 `infeasible` 任务类型作为"不该做"的干扰） | `run_multienv.py` 多 VM 并行；README 称云 provider 可把评测压到 1 小时内 |
| **τ-bench** | 内存 dict 数据库（业务实体）+ user simulator 对话 | `self.data = self.data_load_func()` 全量重载 | 终局：`sha256(data) == sha256(GT 动作重放后 data)` + 必需输出包含 | 无（user simulator 有随机性，但那是行为扰动不是状态扰动） | i.i.d. k 次试验（`pass^k`），进程内串行 |
| **AgentBench** | 8 个异构环境（OS/DB/KG/卡牌/网购…），每 sample 一个容器 session | 释放/重新分配容器（Redis 分配）；`/cancel` 显式释放 | 任务级 metric，`calculate_overall` 汇总状态计数 | 无 | worker `capacity-current` 背压；**并发不安全点**：`NETWORK_ERROR` 不 cancel → 容器泄漏 |
| **SWE-bench** | 一个 docker 容器（repo @ base commit） | **每实例新建容器**，用后即弃；ghost container 清理 | `FAIL_TO_PASS` 全过 **且** `PASS_TO_PASS` 不回归 → `resolved` | 无 | 多容器并行（run_id 隔离） |
| **SWE-agent** | 容器文件系统 + git 工作区 | `reset()` = `cd /` + copy repo + git checkout base；`hard_reset()` = 重启 deployment | 下游基准（SWE-bench/HumanEvalFix）pass@1 | 无 | 每实例独立 deployment |
| **TheAgentCompany** | 175 个任务 docker 镜像内的完整公司环境（内部网站 + DB + NPC） | 重建容器 + 恢复备份数据（"Quick system reset in minutes"）；`/utils/init.sh` | `eval.py`：结果判定为主 + **subcheckpoints 部分分**；确定性 + LLM 两类 evaluator | **有**：NPC 同事会发消息/改文档（"Multiple Agent Interaction"、镜像含 `npc`） | 多容器 |
| **AppWorld** | 9 个 app 的 DB（每任务一套 `data/tasks/<id>/dbs`） | 任务 DB 重新载入；`clear_remote_dbs_cache` | **state-based unit tests** + **collateral damage** 检查 | 有：`~100` 个虚构用户的既有活动数据构成背景状态 | 单进程为主 |

**四点必须单独说明的机制**：

1. **τ-bench 的 `pass^k`**：定义取自论文 HTML 正文——"the chance that **all k** i.i.d. task trials are successful, averaged across tasks"，明确与 `pass@k`（至少一次）相对。论文报告 retail 上 `pass^8 < 25%`。**对 Atlas 的意义**：任何"自适应/恢复"能力若不用 `pass^k` 衡量，就无法区分"偶尔修好"与"稳定正确"。
2. **WebArena 的自托管**：五个站点是真实开源软件（GitLab、Magento 系电商、Postmill 论坛、Kiwix），意味着**状态语义由这些软件决定**——不可枚举、不可快照（除整容器重建），因此只能事后判分。这是"环境太真反而不能建模"的典型。
3. **OSWorld 的 VM snapshot**：唯一真状态快照。`snapshot_name="init_state"`、`_revert_to_snapshot()`、`is_environment_used` 的惰性策略、以及 "Setup may mutate the guest before failing. A later retry or task must restore the snapshot even if setup never completes" 这条注释——**承认"失败前可能已经改了状态"，因此失败路径也必须回滚**。这是全部 9 个基准里唯一一处显式的"部分成功"语义。
4. **SWE-bench 的容器化**：用"每实例一容器、用后即弃"把状态问题变成资源问题（慢但无歧义）。`sweb.eval.<instance>.<run_id>` 的命名里带 `run_id`，本身就是并发隔离的键。

**共同的空白**：**没有任何基准定义"环境状态版本"**。因此：
- 无法构造"读到的状态已过期"的测试用例（除了 TheAgentCompany 的 NPC 间接制造）；
- 无法评测"agent 是否能发现自己看到的是旧的"；
- 也就无法评测 Atlas 想做的核心能力。**这是 RQ3 最重要的基准缺口，也是一个可发表的贡献点。**

---

## 5. 对 Atlas 的设计建议：PendingToolCall 状态机 + 观测失效与对账

### 5.1 现状与缺口（有代码依据）

- `atlas/orchestration/state_machine.py`：`TaskState = {PLANNING, EXECUTING, AUDITING, REPAIRING, PAUSED, COMPLETED, FAILED}` + `LEGAL_TRANSITIONS` + `_checkpoint_sink`（"每次成功转移后回调一次快照"）。**这是任务级状态机，粒度是"任务阶段"，不是"单次工具调用"。**
- `atlas/tools/protocol.py`：`ERROR_KINDS = (timeout, invalid_request, no_evidence, budget_exhausted, upstream_error)` —— **五类全部是"确定"失败，缺 `unknown_commit`。**
- `atlas/tools/protocol.py::build_idempotency_key(session_id, subtask_id, attempt=1)` 返回 `f"{session_id}:{subtask_id}:attempt-{attempt}"` —— **把 attempt 编进键，与幂等语义相反**（见 §3.3 IETF 原文）。同一逻辑请求重试时键变了，下游无法去重。
- `atlas/storage/events.py` 已有 `node_expired(node_id, reason)` 与 `node_superseded(old, new, reason)` —— **"过期"语汇已存在，但只作用于记忆节点，不作用于工具调用。**

### 5.2 PendingToolCall 状态枚举

建议 7 态，**与 `TaskState` 正交**（工具调用状态不改变任务阶段，只在 `EXECUTING`/`REPAIRING` 内部流转）：

| 状态 | 含义 | 是否可重启恢复 |
|---|---|---|
| `PROPOSED` | 计划已产出，尚未写到持久介质 | 否（可安全丢弃，无副作用） |
| `DISPATCHED` | **已持久化 + 已发出网络请求**，未收到响应 | 是（重启后按对账处理） |
| `SETTLED` | 收到响应且结果可用 | 是 |
| `FAILED_DETERMINATE` | 确定失败（对应现有五类 `ERROR_KINDS`） | 是 |
| `FAILED_PARTIAL` | 部分成功（副作用已部分生效） | 是（只能补偿） |
| `INDETERMINATE` | **未知提交**：已过 Start-To-Close 但无响应 | 是（唯一合法后继是 `RECONCILING`） |
| `RECONCILED` | 对账完成，带 `outcome ∈ {APPLIED, NOT_APPLIED, UNKNOWABLE}` | 是 |

要点：**`DISPATCHED` 必须先落盘再发请求**（transactional outbox 的语义：本地日志写入是提交点，网络发送是提交之后的事）。**"已发出"这个事实本身不能只存在内存里**——OpenHands 的 `_pending_action_info` 就是反例。

### 5.3 转移条件

- `PROPOSED → DISPATCHED`：`checkpoint` 写入成功之后才允许发请求（顺序不可交换）。
- `DISPATCHED → SETTLED`：收到带 `tool_call_id` 的响应。
- `DISPATCHED → FAILED_DETERMINATE`：收到明确错误码/异常，**且能判定未执行**。
- `DISPATCHED → FAILED_PARTIAL`：响应声明部分成功，或对账发现部分生效。
- `DISPATCHED → INDETERMINATE`：**Start-To-Close 超时**（不是"总超时"）。此处必须停住，**禁止直接重试**。
- `INDETERMINATE → RECONCILED`：对账返回三分支之一。
- `FAILED_PARTIAL → COMPENSATING`：启动补偿动作（补偿动作本身必须是 `PendingToolCall`，递归使用同一状态机——依据 Azure 的 "Define the steps in a compensating transaction as idempotent commands"）。

### 5.4 超时与对账策略

照搬 Temporal 的三元组，但映射到工具调用：
- `dispatch_timeout`（≈ Schedule-To-Start）：从"入队"到"确认发出"，超时 → 直接 `FAILED_DETERMINATE`（肯定没发出去）。
- `exec_timeout`（≈ Start-To-Close）：从"确认发出"起算，超时 → **`INDETERMINATE`**（可能执行了）。
- `total_timeout`（≈ Schedule-To-Close）：含重试的总预算，超时 → 升级（人工/降级）。
- `heartbeat`：长任务（如浏览器操作、大文件写入）要求工具周期性上报进度，**心跳间隔 × 3 无心跳 → `INDETERMINATE`**（而非判定失败）。

对账（reconciliation）建议"三探针"顺序，代价从低到高：
1. **幂等键反查**：用同一个 `idempotency_key` 调工具的"查询/去重"入口，服务端应返回首次结果（Stripe 模式）。
2. **状态读回**：读目标对象（订单状态、文件哈希、网页元素），与"执行前快照 + 预期后置条件"比对。这一步要求 Atlas 在 `DISPATCHED` 时就记下**前置状态指纹**（否则无法判断变化是不是自己造成的）。
3. **日志反查**：查工具侧的审计日志（AppWorld 的 `api_calls.jsonl` 是这个思路的现成形态）。
三探针都给不出结论 → `outcome = UNKNOWABLE`，**必须落到记忆里成为"不可判定事实"**，而不是假装成功或失败。

### 5.5 幂等键设计（须修现有实现）

- 键 = `hash(session_id ‖ subtask_id ‖ tool_name ‖ canonical_json(args))`，**不含 attempt**。
- `attempt` 作为独立字段随请求发送，仅用于退避与观测。
- 键带 TTL 与 fingerprint：同键不同 payload 必须被拒绝（IETF 的 "Idempotency Fingerprint"）。
- **感知类（只读）工具可以不带幂等键**，副作用类必须带（CoALA 的"内部动作 vs 外部动作"二分在此落地）。
- 补偿动作单独一个键空间，前缀 `comp:`。

### 5.6 与 checkpoint / replay 的关系

1. **PendingToolCall 必须进 checkpoint**。`CheckpointRow` 增加一个 `pending_calls` 列（JSON 数组），每条含 `state / idempotency_key / attempt / dispatched_at / deadline / precondition_fingerprint / last_heartbeat`。
2. **replay 遇到 `DISPATCHED` 无对应 observation → 强制转为 `INDETERMINATE` 并触发对账，绝不能静默重放**。这是 replay 语义的分水岭：事件溯源可以重放"计算"，不能重放"副作用"（Temporal 把副作用隔离进 Activity 就是为此）。
3. **`checkpoint` 的写入顺序**照抄 OpenHands `EventStream.add_event` 的策略：**先写单条记录，再写聚合索引/缓存页**；读取时缓存缺失只是回退到慢路径，不会得到错误结果。
4. **`node_expired` 复用**：对账得到 `UNKNOWABLE` 时，把依赖该动作的下游记忆节点批量 `node_expired(reason="pending_call_unknown")`，让"基于过期信息继续执行"在事件层面可见、可审计。

### 5.7 观测失效与"避免按过期信息行动"

四层防线，从弱到强：

1. **观测元数据**：每次观测记 `observed_at`、`source_version`（ETag / DB `updated_at` / git SHA / 页面 hash）、`ttl`。`EvidenceItem` 增加这三个字段（当前只有 `source/chunk_id/quote/score/retriever/rank`）。
2. **读写一致性**：自己刚写的东西必须能读回自己写的版本（read-your-writes，Cosmos 的 session consistency）。若读回版本低于自己写的版本 → **立即判为 stale，禁止使用**。
3. **陈旧写拒绝**：给每个"写动作"带单调 `epoch`（fencing token 的 agent 版）。下游若发现 epoch 小于已见最大值，**拒绝执行**。这样即使 agent 用过期认知发了写请求，世界也不会被破坏。
4. **决策前的 stale 检查**：进入 `AUDITING` 前，对所有被引用的观测做一次"版本是否仍是决策时版本"的校验；不一致 → 打回 `REPAIRING` 并显式记录"因观测过期而重规划"。**这一步是 Atlas 目前完全缺失的闭环。**

**一个必须承认的边界**：第 3 层要求下游支持 epoch，Atlas 无法单方面对第三方 API 施加。因此现实策略是**分层**：对 Atlas 可控的工具（Atlas 自己封装、Atlas 自己的存储）用 epoch；对不可控的第三方用"前置指纹 + 读回比对 + 声明式 TTL"，并接受残余风险（在报告与文档中显式声明，而非假装已解决）。

---

## 6. 元数据核验表

venue 一栏**原样抄自工具返回**。`venue-verified.md` 已覆盖的以该表为准；本会话新增核实的另行注明来源。凡本会话与核验表冲突者，双记并标 ⚠️。

| 简称 | 标题 | 年份 | venue（工具原样） | 链接 | 核验方式 |
|---|---|---|---|---|---|
| WebArena | WebArena: A Realistic Web Environment for Building Autonomous Agents | 2023/2024 | ICLR 2024 poster | https://arxiv.org/abs/2307.13854 | OpenReview notes/search `content.venue.value`（本会话独立命中，与核验表一致） |
| BrowserGym | **The BrowserGym Ecosystem for Web Agent Research** | 2024/2025 | Trans. Mach. Learn. Res. 2025 | https://arxiv.org/abs/2412.05467 | OpenReview `content.venue.value`；arXiv abs 页标题实测（**不是** "A Gym Environment…"） |
| OSWorld | OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments | 2024 | NeurIPS 2024 Track Datasets and Benchmarks Poster | https://arxiv.org/abs/2404.07972 | OpenReview `content.venue.value`（本会话独立命中；另有 "NeurIPS 2024" 记录） |
| τ-bench | τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains | 2024/2025 | ICLR 2025（poster）⚠️ | https://arxiv.org/abs/2406.12045 | 依 `venue-verified.md`（OpenReview）。⚠️ **本会话 OpenReview 未命中，且官方 README 的 BibTeX 为 `@misc{yao2024tau, eprint={2406.12045}}`（纯 arXiv）**，口径待统一 |
| AgentBench | AgentBench: Evaluating LLMs as Agents | 2023/2024 | ICLR 2024 poster | https://arxiv.org/abs/2308.03688 | `venue-verified.md`（arXiv Comments + OpenReview）；本会话 OpenReview 独立命中 "ICLR 2024 poster" |
| SWE-bench | SWE-bench: Can Language Models Resolve Real-World GitHub Issues? | 2023/2024 | ICLR 2024 oral | https://arxiv.org/abs/2310.06770 | OpenReview `content.venue.value`（本会话独立命中，"ICLR 2024 oral"） |
| SWE-agent | SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | 2024 | NeurIPS 2024 poster | https://arxiv.org/abs/2405.15793 | OpenReview `content.venue.value`（本会话独立命中） |
| OpenHands | OpenHands: An Open Platform for AI Software Developers as Generalist Agents | 2024/2025 | ICLR 2025 Poster | https://arxiv.org/abs/2407.16741 | OpenReview `content.venue.value`（**本会话新增核实，核验表未收录**） |
| Voyager | Voyager: An Open-Ended Embodied Agent with Large Language Models | 2023 | Accepted by TMLR | https://arxiv.org/abs/2305.16291 | `venue-verified.md`；本会话 OpenReview 亦命中 "Accepted by TMLR" |
| ReAct | ReAct: Synergizing Reasoning and Acting in Language Models | 2022/2023 | ICLR 2023 | https://arxiv.org/abs/2210.03629 | arXiv abs 页 comments 原文 "v3 is the ICLR camera ready version with some typos fixed."（本会话实测抓取） |
| Toolformer | Toolformer: Language Models Can Teach Themselves to Use Tools | 2023 | NeurIPS 2023 oral | https://arxiv.org/abs/2302.04761 | `venue-verified.md`（OpenReview）；OpenAlex venue="neural information processing systems"、DOI `10.52202/075280-2997` 佐证 |
| LATS | Language Agent Tree Search Unifies Reasoning Acting and Planning in Language Models | 2023/2024 | ICML 2024 | https://arxiv.org/abs/2310.04406 | `venue-verified.md`（PMLR v235 TOC 命中；OpenReview 仅 "Submitted to ICLR 2024" = 投稿记录，非录用） |
| Reflexion | Reflexion: Language Agents with Verbal Reinforcement Learning | 2023 | NeurIPS 2023 poster | https://arxiv.org/abs/2303.11366 | `venue-verified.md`（OpenReview） |
| ReWOO | ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models | 2023 | arXiv preprint | https://arxiv.org/abs/2305.18323 | `venue-verified.md`（arXiv Comments 无会议信息）；OpenAlex venue="arXiv (Cornell University)" |
| CoALA | Cognitive Architectures for Language Agents | 2023/2024 | TMLR | https://arxiv.org/abs/2309.02427 | `venue-verified.md`（arXiv comments "v3 is TMLR camera ready" + OpenReview "Accepted by TMLR"） |
| WorkArena | WorkArena: How Capable are Web Agents at Solving Common Knowledge Work Tasks? | 2024 | ICML 2024 Poster | https://openreview.net/forum?id=BRfqYrikdo | OpenReview `content.venue.value`（**本会话新增核实**）；仓库 https://github.com/ServiceNow/WorkArena 实测存在 |
| TheAgentCompany | TheAgentCompany: Benchmarking LLM Agents on Consequential Real World Tasks | 2024/2025 | ⚠️ 冲突：`venue-verified.md` 记 arXiv preprint；OpenReview="NeurIPS 2025 Datasets and Benchmarks Track poster"；OpenAlex venue="neural information processing systems"（DOI `10.52202/085713-0315`, type=conference-paper） | https://arxiv.org/abs/2412.14161 | 三源并列，待 Lead 统一。**建议按 NeurIPS 2025 D&B 采信** |
| AppWorld | AppWorld: A Controllable World of Apps and People for Benchmarking Interactive Coding Agents | 2024 | ACL 2024 | https://arxiv.org/abs/2407.18901 | `venue-verified.md`（arXiv Comments "ACL'24 Camera Ready"）；本会话 OpenReview 亦命中 "ACL 2024" |

### 待核实清单

1. **τ-bench 的 venue**：核验表记 ICLR 2025 poster，但本会话 OpenReview 未命中、官方 README BibTeX 为纯 arXiv `@misc`。建议 Lead 复核 OpenReview forum id。
2. **TheAgentCompany 的 venue**：三源冲突（见上）。若采信 NeurIPS 2025 D&B，需同时修正核验表。
3. **OpenHands 的版本边界**：本报告实现结论绑定 `1.0.0` tag；`main` 已转为前端 monorepo，更高版本的 `_pending_action` 是否持久化**未核实**。
4. **AgentBench 服务端实现**：仅读客户端协议（`src/client/task.py`）与 README；服务端 controller/worker 的 session 状态机与容器回收策略**未读源码**。
5. **WorkArena 的 validator 实现**：venue 与仓库已核实，validator 细节未抓取。
6. **Toolformer/LATS/ReAct/CoALA 的机制细节**：元数据高可信，机制描述基于摘要与通行理解，**未逐节精读原文**（对应条目已标可信度）。
