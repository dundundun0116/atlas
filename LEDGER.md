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


## 变更登记（插件自动维护）

> 这张表由插件**自动**维护：每次写台账 / 归档追加一行，只留最近 12 条（条数可在设置里调，项目清单里的 `changeRegisterMax` 优先）。
> 完整历史在活动日志的 `L0-ledger-changes.md`（用 `ledger_log({view:"audit"})` 查）。

- 2026-10-28 19:53:27 (append) [486c] +13 行 → 163 行 · revision→39
- 2026-10-28 16:39:52 (append) [486c] +7 行 → 150 行 · revision→38
- 2026-10-28 16:38:00 (append) [486c] +7 行 → 143 行 · revision→37
- 2026-10-28 16:34:52 (append) [486c] +7 行 → 136 行 · revision→36
- 2026-10-28 16:31:53 (append) [486c] +6 行 → 129 行 · revision→35
- 2026-10-28 16:30:08 (append) [486c] +11 行 → 123 行 · revision→34
- 2026-10-28 13:30:01 (append) [486c] +6 行 → 112 行 · revision→33
- 2026-10-28 13:29:41 (append) [486c] +11 行 → 106 行 · revision→32
- 2026-10-28 13:29:18 (append) [486c] +14 行 → 95 行 · revision→31
- 2026-10-28 13:28:44 (append) [486c] +8 行 → 81 行 · revision→30
- 2026-10-28 13:28:02 (append) [486c] +7 行 → 73 行 · revision→29
- 2026-10-28 13:26:08 (replace) [486c] -176 行 → 66 行 · revision→28
