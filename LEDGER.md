# CompassRAG 活跃台账

> 当前状态（可改写/压缩）。历史进 `JOURNAL.md`；长决策进 `decisions/D<n>.md`；坑与机制知识进 `lessons/`；目标树进 `goals/`。
> ⚠ 根目录 `DECISIONS.md` / `LESSONS.md` / `GOALS.md` 是卡片化迁移前的**快照**，不要读、不要写。

## 一句话
单人多跳 QA 的检索决策 Agent：LLM 只提路由、查询与断言，确定性代码负责召回、引用校验、失败分类与记录；`scripted` 与 `decision` 共用同一状态机（默认走 LangGraph），证据核验是可选层。

## 当前阶段：按 `docs/COMPASSRAG_AGENT_UPGRADE_PLAN.md` 的阶段 A–E

| 阶段 | 状态 | 证据 |
|---|---|---|
| A 统一状态与工具协议 | ✅ | `agent/schemas.py`、`tools.py`（六类 outcome）、`state.py`（四类预算）、`ResearchRequest/Result` + `research.py`；测试 14 + 10 |
| B 证据闭环与有限修复 | ✅ | `diagnose/verifier.py`（claim + 代码校验引用）、`repair.py`（五分类 + 一对一动作 + 上限 + 真 `switch_view`）；测试 21 |
| C LangGraph 控制流 | ✅ | `agent/graph.py`，`use_graph` 默认 True，`eval_qa.py --no-graph` 对照；假 LLM 13 条 + 真 LLM 抽查（证据/轮数/stop/prompt 逐位一致） |
| D 可回放 trace 与报告 | ◐ | `eval/{traces,manifest,report}.py`、六配置重跑、全量回放验证；**缺三基准与同预算对照（需付费）** |
| E 服务化与 Atlas adapter | ✅ | `api/`（/research、/runs/{id}、/health 含索引版本）、`adapters/atlas.py`、`scripts/serve_api.py`；测试 14 |

## 代码位置
- 控制流 `agent/core.py`（顺序实现，对照）｜`agent/graph.py`（LangGraph，默认）
- 契约 `agent/schemas.py` ｜ 工具协议 `agent/tools.py` ｜ 状态预算 `agent/state.py` ｜ 对外适配 `agent/research.py`
- 证据闭环 `diagnose/{verifier,repair}.py` ｜ 服务化 `api/` ｜ Atlas adapter `adapters/atlas.py`
- 评测 `eval/{metrics,runner,traces,manifest,report}.py`
- 测试 `tests/`：**219 passed**（全部无需真实 API）

## 六配置结果（MuSiQue samples 50 题 / 2026-10-28 重跑 / 本批 279.5 万 token）

| 配置 | EM 严格 | EM 宽松\* | F1 | 召回@10 | full-hit@10 | token/题 | 轮 | 拒答 |
|---|---|---|---|---|---|---|---|---|
| naive | 0.300 | 0.440 | 0.432 | 0.720 | 0.460 | 2,162 | 1.00 | 34% |
| +rewrite | 0.400 | 0.560 | 0.575 | 0.720 | 0.460 | 3,540 | 1.00 | 20% |
| +kg | 0.360 | 0.540 | 0.512 | **0.813** | **0.600** | **2,022** | 1.00 | 28% |
| +decision | 0.440 | 0.620 | 0.622 | 0.523\*\* | 0.220\*\* | 11,935 | 2.06 | 10% |
| +full | 0.460 | 0.600 | 0.599 | 0.522\*\* | 0.240\*\* | 11,834 | 2.12 | 18% |
| +verify | 0.420 | **0.640** | 0.604 | 0.542\*\* | 0.280\*\* | 20,087 | 2.04 | 12% |

\* 宽松 EM 只是诊断口径（D10），**对外与简历只用严格 EM**。
\*\* 决策行证据逐轮累积，@10 只算第一轮，名义值偏低。

**三条判读**：① 决策层 +14pp 严格 EM，代价 5.5× token；② 实体图是唯一提升证据覆盖的一路（召回 0.720→0.813）且最省 token；③ 核验层严格 0.420 / 宽松 0.640（最高），**结论取决于口径**——它是可信度模块，不是提分模块。

## 硬约束（违反 = 返工或数据作废）
- 锚点判据：**证据 id / 召回 / prompt_tokens 逐题一致**（硬）+ **严格 EM ±0.04、F1 ±0.05**（容差）；token/题与空答率随模型行为漂移，只记录（`D6`/`D7`）。
- 结构化输出的"预算阶梯 + 解析失败安全降级"不可移除（`D2`/`L1`）。
- 答案协议不得与核验合并成一次调用（`D4`）。
- `compassrag/agent.py` 与 `compassrag/agent/` 不可并存（`L2`；旧模块已删）。
- 任何"模块无效"的结论先排除口径因素再下（`D10`）。
- `runs/qa/` 里 `_invalid_` 前缀是作废口径；旧口径行在 `runs/qa/_legacy/`，与带轨迹的新行不要混算（`L3`）。

## 待批花费（不擅自跑）
1. 答案格式实验 B 臂（`configs/ablation/plus_full_words.yaml`，约 60 万 token）——见 `docs/experiment-answer-format.md`
2. 同预算对照（naive vs plus_full 固定 token 预算，约 20–30 万）
3. 三基准（≥300 万；2wiki/hotpotqa 需先本地建语料与索引，实体图另计）

## 其他未决
- 仓库 ~40 个文件**未 commit**（等用户决定是否提交）。
- `plan accuracy` 只有弱代理（路由 vs 跳数 0.980），无人工标注计划（`D9`）。
- `health` 的索引版本是块 id 前缀哈希，未与 manifest 自动比对。

## 台账设施
- `decisions/D1–D10` ｜ `lessons/L1–L5 · E1–E3` ｜ `goals/g1–g4`（自动回合内无法编辑，以本台账为准）
- `JOURNAL.md` 流水（不注入）｜ `_ledger-archive/` ｜ `_ledger-log/`（gitignore）｜ `_handoff/`（两份，最新为 13:22 更正版）
- `PRINCIPLES.md` 已登记路径但未创建——只有用户能写。

## 下一步
优先级取决于预算：批 → 先跑答案格式 B 臂（能同时解释"严格低/宽松高"）；不批 → 整理决策卡索引、补 README 交叉引用、准备提交清单。

## 2026-10-28 真实索引 API 冒烟 + 契约修正

- `scripts/serve_api.py` 增加 `--check`：装配真实索引后打印 `/health` 的索引规模与版本就退出（**不监听端口、不调模型**）。实测：bm25/dense 各 4,646 块且版本指纹相同（`4646:14d7eca96240`）、kg 41,254 节点。
- **真实索引 + 假 LLM 的全链路冒烟**（零成本）：`POST /research` → 200，返回 5 条证据、首条 `Wooden Heart (novel)`/`bm25`/rank 4、quote 为真实原文；`GET /runs/{id}` 可回读、steps = `[retrieve, answer]`。
- **修掉一个会误导上游的契约问题**：模型回 `unknown` 时此前 `failure=""`、`confidence>0`——等于告诉 Atlas"成功且挺可信"。现在 `is_answerless()` 生效：`unknown`/空答 → `failure=no_evidence`、`confidence=0`（证据仍照常返回，说明"证据有，但没答出来"）。复跑冒烟确认。
- 测试 **221 passed**；README 的服务化小节补了 `--check` 与失败语义。

## 2026-10-28 `--check` 增加 manifest 对账

- `serve_api.py --check` 现在除了打印索引规模/版本，还会把当前语料与**最近一次 run 的 manifest** 对账：`corpus_chunks` 一致打印 `OK`，不一致明确报警并提示"可能换了语料变体或重建过索引"；没有 manifest 时也不装死，提示先跑一次评测。
- 实测两条路径：`plus_full` → `corpus_chunks=4646 实测=4646 → OK`；`plus_full_words`（尚未跑过）→ 明确提示"没有找到 manifest"。
- 这补的是阶段 E 里"索引版本检查"的最后一环：health 暴露指纹、`--check` 与 manifest 对账，Atlas 侧既能在运行时看 health，也能在部署前跑一次体检。
- 顺带修了一个和之前同类的坑：`_check_manifest` 一开始被追加在 `if __name__` 之后，`main()` 先执行导致 NameError——**脚本里的函数定义必须在 `__main__` 之前**（已记入本轮教训）。
- 测试 **221 passed**；仓库 **50 个文件未提交**（比上轮多，因为新增 docs/configs/tests）。

## 2026-10-28 token 效率前沿（零成本，补"同预算"的直觉）

- `eval/report.py` 新增 `cost.em_per_1k_tokens`（EM ÷ (token/题 / 1000)），补两条测试（含除零保护）。
- 六配置效率（严格 EM / token 每千）：
  - naive 0.300 / 2,162 → **0.139**
  - +rewrite 0.400 / 3,540 → 0.113
  - **+kg 0.360 / 2,022 → 0.178（最高）**
  - +decision 0.440 / 11,935 → 0.037
  - +full 0.460 / 11,834 → 0.039
  - +verify 0.420 / 20,087 → 0.021
- **判读**：实体图是唯一**同时**提升准确率、证据覆盖并降低成本的组件（比 naive 还便宜 140 token/题）；决策层是"5.5× token 换 +14pp"；核验层是"9× token 换一个口径存疑的收益"。
- **边界写清楚**：这是**跨配置效率对照**，不是"把决策层压到 2.5k token 还能保持 0.44"——真正的同预算对照要固定预算重跑（仍待批准）。把它当成"哪条路值得继续投入"的决策依据，而不是因果结论。
- 测试 **223 passed**；README 结果区补了这一条。

## 2026-10-28 收口：无预算可做的事已做完

- 终检：**223 passed**；六配置结果可从 `runs/qa` 重算出同一组数字（naive 0.300 / rewrite 0.400 / kg 0.360 / decision 0.440 / full 0.460 / verify 0.420，均 n=50）；仓库 **50 个文件未提交**。
- 不花钱能做的实质工作到此为止。剩下三件**全部依赖用户决策**：
  1. 答案格式实验 B 臂（60 万 token，最有性价比——能一次解释严格/宽松差 0.14–0.22）；
  2. 同预算对照（20–30 万 token）；
  3. 三基准（≥300 万，且需先建 2wiki/hotpotqa 的小库语料与索引）。
- 另有一件非代码决策：是否把当前 50 个文件打成一次 commit（`feat: A–E 升级 + 六配置基线`）。
- **决定把 goal 标为 blocked**：blocking condition 已连续超过 3 轮未变（预算未批 + 提交未定），继续自动续跑只会产生低价值改动。等用户回来后解除即可，台账与交接单里都写清了从哪继续。
- 交接单一共两份，最新是 `_handoff/2026-10-28-1322-更正：…`（含双口径更正）。

## 2026-10-28 回合收口：goal 转 blocked

- 已把 goal 标为 **blocked**（revision 3），原因写死在 goal 里：计划内剩余三项全依赖用户决策（三笔付费实验的预算、50 个文件的提交决定），且该条件已连续 9+ 轮未变。
- 自动续跑到此结束（40 轮上限里用了 18 轮）。**恢复方式：用户给一句预算/提交决定后，直接把 goal 改回 active 即可**，台账第 125 行起的"待批花费"就是继续清单。
- 交付物状态（最后一次核验）：223 tests 全绿；六配置结果可从 `runs/qa` 重算出同一组数字；`runs/traces/` 300 条轨迹零解析失败、naive 50/50 可回放复现；README / STATUS / failure-cases / interview-qa / 实验设计 五份文档齐；两份交接单（最新 13:22 更正版）。

## 2026-10-28 答案格式实验（B 臂）已跑完：H1 成立

- **结果**：`plus_full` 0.460/0.600（严格/宽松）→ `plus_full_words`（≤5 词）**0.600/0.660**，F1 0.599→0.684，token/题 11,834→**11,143**（反而更省），拒答率 18%→14%；**逐题 7 好 0 坏**。本臂 557,165 token。
- **判定**：预注册阈值（严格 ≥+0.08 且宽松不降）→ **H1（措辞问题）**，判定代码在 `compassrag/eval/arms.py`，命令 `scripts/compare_arms.py`。
- 结论与后续写进 `decisions/D11` 与 `docs/experiment-answer-format.md`；README 结果区加了这条，并注明**六配置表仍是旧措辞口径**。
- **本节取代上文「待批花费」第 1 条**（B 臂已完成）。新的待批：
  1. **六配置用新约束重跑**（≈280 万 token）——只有这样才能对外报"真实严格 EM"；
  2. C 臂 `plus_verify` + 词数上限（≈100 万）——检验核验层在不再啰嗦前提下的定位；
  3. 同预算对照（20–30 万）；4. 三基准（≥300 万）。
- 已提交三次 commit（代码/台账/双臂工具），明细见 `git log`。

## 2026-10-28 收尾：提交与实验产物入库

- 工作区已提交 **4 个 commit**（`8b259bd` A–E 升级 → `88ccc21` 台账 → `81c1a80` 双臂工具 → `ff872eb` 实验结论），当前只剩 2 项未跟踪（`_stash/` 已 gitignore 后的残留）。
- B 臂结果文件：`runs/qa/musique__plus_full_words__samples.jsonl`（50 题）与轨迹 `runs/traces/musique__plus_full_words.jsonl`；manifest 指纹 `6a52c881655dc554`。**注意 `runs/` 不入库**，数字以 `docs/experiment-answer-format.md` 与 `decisions/D11` 里的表为准；要复现需重跑（命令在实验文档里）。
- 测试 228 passed（新增 arms 5 条）。下一步仍等预算决定：六配置重跑 280 万（推荐）> C 臂 100 万 > 同预算 20–30 万 > 三基准 300 万+。

## 2026-10-28 STATUS 与交接单同步（H1 之后）

- `docs/STATUS.md` 重写：HEAD `cc4bbac`、228 测试、A–E 逐阶段证据、**H1 实验结论**、以及四条已知边界（六配置仍是旧措辞口径 / 核验定位取决于口径 / plan 只有弱代理 / 三基准与同预算未跑）。已提交（`cc4bbac`）。
- 新交接单：`_handoff/2026-10-28-1634-…（H1 已确认，待六配置重跑）`，写明当前数字、下一步四种花费与各自的理由。
- 工作区干净，HEAD `cc4bbac`；`runs/` 不入库（数字以文档与 `decisions/` 为准）。
- **当前不缺代码、不缺文档、不缺测试，只缺一个预算决定**：是否用 `strict_answer_words=5` 重跑六配置（≈280 万 token）。这是唯一能让公开表严格 EM 反映真实水平的一步。

## 2026-10-28 文档一致性守卫（README ↔ runs）

- 新增 `tests/test_readme_consistency.py`（6 条）：把 README 结果表六行逐列（严格/宽松 EM、F1、召回、full-hit、token/题）与实际 `runs/qa` 复算值比对，**任一方过期就红**；没有 runs 的环境自动跳过，不影响纯代码测试。
- 这条守卫解决的是本项目最容易犯的错：改完代码/重跑数据后忘记更新表格（今天已经发生过一次 196→208→218→228 的计数漂移）。
- 测试 **234 passed**，已提交 `ba761d9`，工作区干净。
- **状态：不花钱能做的实质工作已全部做完**（代码、测试、文档、台账、交接单、一致性守卫）。剩下的每一项都是付费实验：六配置重跑 280 万（首选）、C 臂 100 万、同预算 20–30 万、三基准 300 万+。

## 2026-10-28 回合收口：goal 再次转 blocked（同一阻塞条件）

- goal 标为 **blocked**（revision 5），理由与上轮一致但不花钱的工作已清零：234 tests、HEAD `ba761d9`、工作区干净、文档/台账/交接单/一致性守卫全就绪；计划内剩余四项**全是付费实验**（六配置重跑 ≈280 万 / C 臂 ≈100 万 / 同预算 20–30 万 / 三基准 ≥300 万）。
- 与上一轮 blocked 的区别：那次是"代码侧还有零散收尾"，这次是**零成本工作面已耗尽**——继续自动续跑只能产生低价值改动。
- 恢复路径写在 `LEDGER.md` 的「待批花费」与最新交接单里：用户回一句「重跑六配置」或「先跑 C 臂」即可解除。
- 本轮新增并提交：`tests/test_readme_consistency.py`（README 表 ↔ runs 复算逐列比对，6 条；无 runs 环境自动跳过），提交 `ba761d9`。

## 2026-10-28 收口（用户决定「就这样」）

- 用户决定**停在可交付状态**，剩余四项付费实验（六配置重跑 280 万 / C 臂 100 万 / 同预算 20–30 万 / 三基准 300 万+）**不再启动**。goal 保持 blocked，不再自动续跑。
- 终态：**234 passed**，HEAD `9a37673`，`git status` 干净（0 项）。
- 交付清单（都可在库内直接查到）：
  - 代码：`compassrag/agent/{schemas,tools,state,core,graph,research}.py`、`diagnose/{verifier,repair}.py`、`api/`、`adapters/atlas.py`、`eval/{traces,manifest,report,arms}.py`
  - 脚本：`eval_qa`（含 `--no-graph` / 预算开关 / manifest）、`report_runs`、`check_benchmarks`、`serve_api --check`、`compare_arms`
  - 文档：README（双口径表 + H1 结论）、`docs/STATUS.md`（A–E 证据链 + 四条边界）、`docs/failure-cases.md`（四案例）、`docs/interview-qa.md`（十问答）、`docs/experiment-answer-format.md`（预注册 + 结果）
  - 记忆：`decisions/D1–D11`、`lessons/L1–L6 · E1–E3`、三份交接单
  - 数据（未入库，保留在本地 `runs/`）：六配置 50 题结果 + 300 条轨迹 + B 臂结果
- **未完成且明确记录在文档里**：六配置表仍是旧措辞口径（严格 EM 偏低约 0.14）；三基准与同预算对照未跑；plan accuracy 只有弱代理。
- 恢复方式：回一句「重跑六配置」或「先跑 C 臂」，按 `decisions/D11` 与最新交接单执行即可。

## 2026-10-28 新计划：CompassRAG 全栈平台（Task 1–2 完成）

- 计划来源：`/Users/szx/atlas/docs/superpowers/plans/2026-10-28-compassrag-fullstack-plan.md`（6 个 Task）。**Task 1 产品契约 + Task 2 可观察运行流已交付**，commit `0ce1edc`。
- **契约**：`ResearchSubmit(async_mode)`、`RunSummary`（`queued → running → completed | failed | cancelled`）、`TraceEvent`、`ResearchSession`、`RunEnvelope(summary+trace)`；错误统一走 `ResearchResult.failure` 词表 + HTTP 404/422。
- **事件**：`AgentState.step` 现在**同时**登记轨迹并把结构化事件投影给订阅者（step_id / parent / tool / query_source / outcome / latency_ms / tokens / evidence / round / ts）；`finish` 也会发事件。轨迹仍唯一真源。
- **SSE**：`GET /runs/{id}/events` = 历史回放 + 实时订阅 + keepalive，客户端断开不影响 Agent 继续跑。
- **取消**：`POST /runs/{id}/cancel` 协作式：`cancel_check` 在 plan/retrieve/prep/answer 节点入口检查，取消后不再发起新调用，终态 `cancelled`（`StopReason.CANCELLED`）。
- **查询**：`GET /runs`（列表）、`GET /sessions/{session_id}`（会话分组）。
- 测试 **240 passed**（新增异步/SSE/取消/会话/事件字段 6 条 + 既有 234 全部保持）。
- 已知缺口：异步模式尚未强制执行 `timeout_s`（同步模式有）；`web/` 前端未开始；**前端工具链可用**（node v24.12 / npm 11.6）。
- 下一步：Task 3 前端工作台（Vite + React + TS），或先补 Task 2 的异步超时。

## 2026-10-28 全栈计划 Task 3 完成 + 浏览器端到端验证

- **前端工作台交付**（`web/`，Vite + React + TS，commit `e35012d` 起）：提问/预算、答案（置信度/停止原因/拒答）、证据（来源/检索器/排名/引用）、过程时间线（SSE 实时 + 历史回放）、历史运行（打开既有 trace 不重跑模型）。类型全部由 `scripts/export_openapi.py` + `openapi-typescript` 生成（`web/src/api/types.ts`），组件不复制后端 schema。9 条组件测试 + `tsc --noEmit` + `vite build` 通过。
- **后端配套**：`/research/async`（202 + RunSummary）、`/research/sync`（ResearchResult）、`/runs/{id}/events/json`、运行记录保存结构化 `result`（GET /runs/{id} 直接回读）、异步 `timeout_s` 强制（超时 → failed/failure=timeout）。
- **真实浏览器 E2E（1280×800）**：起真实后端（bm25/dense/kg，4646 块）+ Vite，跑两条路径——
  - 决策多轮：5 条证据（含 kg 命中）、7 步时间线、3253 token；
  - direct：`What is the capital of France?` → **Paris**、417 token。
- **E2E 暴露并修掉三个真问题**（commit `040242e`）：① direct 路由错用"只准用证据"提示词，把常识题逼成 unknown → 新增 `DIRECT_ANSWER_PROMPT`；② direct 无证据时置信度 0 会被读成失败 → 给中性 0.5；③ SSE 时间线把生命周期 finish 与 finish 节点各渲染一次 → 只保留带 `step_id` 的轨迹步骤。
- 测试：后端 **244 passed**，前端 **9 passed**，`npm run build` 通过；工作区干净。
- 下一步：Task 4（文档/语料管理闭环：上传、后台构建、索引版本固定）、Task 5（docker-compose + 架构文档）、Task 6（评测报告与 resume 证据）。

## 2026-10-28 全栈计划 Task 4 完成（文档上传 + 索引版本）

- **后端**（commit `942d5c3`）：`compassrag/api/ingest.py` 文档指纹去重 + txt/md/json 解析 + 分块 + BM25 版本化构建；阶段 `chunk/bm25/dense/entity_graph` 如实记录（未启用写原因，不假装成功）；`ingest_routes.py` 五个端点 `/documents`（POST/GET）、`/index/build`、`/index/versions`、`/index/status`（`runtime_version` vs `built_version`，不一致显式告警）；上传即校验（空内容/未知格式/坏 JSON → 422），构建失败记录不抛 500。
- **前端**（commit `6938b79`）：`CorpusPanel` 展示文档/块/两个版本 + 上传 + 重建 + 错误可见；client 增加类型化调用；**vite proxy 补 `/index`、`/documents`**——漏了会让面板静默降级成"未启用"（E2E 才发现）。
- **真实浏览器验证**：上传 `notes.txt` → 重建索引 → `v20261028211321`（1 文档 1 块；chunk/bm25=ok，dense/entity_graph=skipped 并写原因）；`/index/status` 正确反映。
- 测试：后端 **250 passed**（新增 ingest 6 条），前端 **11 passed**，`vite build` 通过。
- 仍未做：Task 5（docker-compose + `docs/fullstack-architecture.md` + 一条命令启动）、Task 6（`docs/evaluation.md` 与 resume 证据）；Playwright 未固化（当前为组件测试 + 手工 E2E）。

## 2026-10-28 全栈计划 Task 5 完成（一条命令启动 + 容器化 + E2E）

- **一条命令**：`scripts/demo.sh`（本地，不依赖 docker）——起后端 + 前端，就绪探测后打印地址，Ctrl-C 一起退出；`--ingest` 切到上传文档索引。实测 `api=200 / web=200`。
- **容器化**：`Dockerfile`（python:3.12-slim + `.[api]` + healthcheck）、`web/Dockerfile`（vite build → nginx，SSE 关 `proxy_buffering`）、`docker-compose.yml`（前后端 + `data/`、`runs/` 挂载 + 健康检查 + 模型/密钥只从环境变量注入）。**注意：本机 docker daemon 未启动**，只做了 `docker compose config` 校验，镜像未实际构建。
- **架构文档**：`docs/fullstack-architecture.md`（组件/端口、同步与异步两条路径、状态机与失败语义、索引版本固定、可复现性、四条已知边界）；README 增加「一条命令起演示」。
- **运行时版本接线**：`serve_api.py --index-source ingest` 装载最新构建版本（仅 BM25），版本写进 `RunRecord.index_version`；同步运行现在也会建 RunRecord（历史可见），`RunSummary` 增加 `index_version`。
- **E2E 测试**（`tests/test_e2e_ingest_flow.py`，零 API 费用）：上传 → 构建 → 装载该版本 → 提问 → 证据必须来自上传文档 → 运行固定同一版本；重建索引后 `mismatch=true` 且运行时版本不变。
- 测试：后端 **252 passed**，前端 **11 passed**；commit `85db95f`；工作区干净。
- 剩余：**Task 6**（`docs/evaluation.md` + `scripts/run_resume_eval.py`：固定数据集/种子/模型/预算/索引版本，五组配置，两次重复，指标可回 trace）。另：Playwright 未固化；docker 镜像未实机构建。

## 2026-10-28 全栈计划 Task 6：评测脚本就绪，正式跑数进行中

- **新增**（commit `d1f6c85`）：`scripts/run_resume_eval.py`（五组配置 × N 次重复，`--parallel` 并发，逐次留 manifest 与 trace，汇总写 `runs/eval/summary.json` 含均值/标准差）；`eval_qa --strict-answer-words`（统一加词数约束，依据 `D11`）；`docs/evaluation.md`（固定条件、五组配置、四列指标、复现命令、五条口径纪律）；README 增加评测一节。
- **正在跑**：`--repeats 2 --parallel 3 --strict-answer-words 5`，日志 `runs/logs/resume_eval.log`，产物 `runs/eval/r1|r2/`。
  - r1 进度：naive 50/50 ✅、plus_kg 50/50 ✅、plus_rewrite 11/50、plus_decision 8/50、plus_verify 3/50（后两个是慢配置）。
  - 用量节奏：约 40 万 token / 10 分钟；按当前配置估算全程 **约 400–500 万 token**（用户「直接做吧」授权继续）。
- 跑完后要做：核对 naive 是否仍 ≈0.300 锚、生成 `summary.json`、把五组均值±标准差写进 `docs/evaluation.md` 与 README（注明口径），再用 `report_runs.py` 交叉核对。
- 注意：`runs/eval/` 与 `runs/qa/` 是两套目录——前者是正式重复实验，后者是历史基线；不要混算。

## 2026-10-28 项目全貌文档（供外部审查）+ 正式评测 r1 出数

- **新增 `docs/PROJECT-STATE.md`（212 行，commit `cd20f02`）**：给不了解项目的审查者看的一页全貌——定位、架构与数据流、代码地图、两套计划（A–E / Task 1–6）完成度、三套评测数字（历史无约束 / 答案格式实验 / 正式评测 r1）、关键决策 D1–D11、9 条已知边界、复现命令、证据地图（claim → 出处）、8 条「可质询清单」。**明确标注 r2 进行中、单次重复不足以作结论。**
- **正式评测 r1 完成（加 ≤5 词约束，n=50）**：naive 0.400/0.460（严格/宽松）、rewrite 0.560/0.620、kg 0.480/0.560（召回 0.813）、decision 0.540/0.640、verify 0.520/0.620。**每个配置都比无约束口径高 0.10–0.16**，与 B 臂单配置实验一致 → 措辞压低是全局现象。
- **修了 `run_resume_eval.py` 的 variant 后缀 bug**：汇总时读文件名没带 `__samples`，导致 `summary.json` 为空；已修，r2 结束后用 `--report-only` 重算。
- r2 进行中（naive/kg 已满 50，rewrite 49，decision 45，verify 21/50）；`verify` 最慢（每题多 1–2 次调用）。
- 测试 252 后端 / 11 前端；工作区干净。


## 变更登记（插件自动维护）

> 这张表由插件**自动**维护：每次写台账 / 归档追加一行，只留最近 12 条（条数可在设置里调，项目清单里的 `changeRegisterMax` 优先）。
> 完整历史在活动日志的 `L0-ledger-changes.md`（用 `ledger_log({view:"audit"})` 查）。

- 2026-10-28 22:27:21 (append) [486c] +8 行 → 221 行 · revision→45
- 2026-10-28 21:31:47 (append) [486c] +9 行 → 213 行 · revision→44
- 2026-10-28 21:22:22 (append) [486c] +10 行 → 204 行 · revision→43
- 2026-10-28 21:16:09 (append) [486c] +8 行 → 194 行 · revision→42
- 2026-10-28 20:59:06 (append) [486c] +11 行 → 186 行 · revision→41
- 2026-10-28 20:46:04 (append) [486c] +12 行 → 175 行 · revision→40
- 2026-10-28 19:53:27 (append) [486c] +13 行 → 163 行 · revision→39
- 2026-10-28 16:39:52 (append) [486c] +7 行 → 150 行 · revision→38
- 2026-10-28 16:38:00 (append) [486c] +7 行 → 143 行 · revision→37
- 2026-10-28 16:34:52 (append) [486c] +7 行 → 136 行 · revision→36
- 2026-10-28 16:31:53 (append) [486c] +6 行 → 129 行 · revision→35
- 2026-10-28 16:30:08 (append) [486c] +11 行 → 123 行 · revision→34
