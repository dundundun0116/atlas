# 回合日志（只追加）

## 2026-10-28 · 抽取 CompassRAG 单项计划 + 台账初始化

- 从双项目路线图抽出 CompassRAG 单项计划：`2026-10-28-compassrag-upgrade-plan.md`（324 行 / 26 个实施项）。
- 核了现状：116 测试全绿 1.25s；汇总五行消融表（naive 0.300/0.422/3927 … +full 0.500/0.621/14682）；发现 `full_hit_at_k` 已实现但 `scripts/eval_qa.py` 没调用；`runs/qa` 只落指标行、无轨迹可回放。
- 与原计划 4 处差异：迁包（同名包遮蔽）、trace 格式前置、答案协议不改（二次校验）、boundary 文档降级；4 个待拍板项留在计划 §9。
- 初始化项目台账：LEDGER / GOALS / DECISIONS / LESSONS / JOURNAL + `_ledger-archive` + `_ledger-log`（日志不入库）。
- 未改动仓库任何源码，未提交 git。

## 2026-10-28 · M0+M1 实施（轨迹格式 + 工具调用化）

- 用户授权"按升级计划做"，自本轮起按 `2026-10-28-compassrag-upgrade-plan.md` 实施；据此把计划 §9 中已带推荐项的四个待定项按推荐执行（迁包、二次校验方向、三基准降级口径留待 M3 确认），LangGraph 单独立卡暂缓（`decisions/D3`）。
- M0：新增 `compassrag/eval/traces.py`——trace 与派生指标两层落盘，`derive_row` / `replay_rows` / `aggregate`；回放不请求模型即可换 k、换金标重算。
- M1：`agent.py` → `agent/{schemas,tools,state,core}.py`；统一工具协议与六类 outcome；scripted/decision 收敛到单条状态机；新增 21 个测试（工具协议 14 + 轨迹回放 7），全套 137 passed。
- 行为等价校验：`git show HEAD:compassrag/agent.py` 取回第 1 代实现，在真实 BM25 索引上对比 naive/rewrite/decision，evidence/rounds/queries/n_llm_calls/tokens 逐位一致；校验后删除临时模块。
- 顺带修掉"工具异常中断整轮评测"与"`full_hit_at_k` 已实现但评测没调用"两处缺口（后者在 `scripts/eval_qa.py` 的新聚合列里接上）。
- 未跑任何付费 LLM 实验；仓库改动未提交 git。

## 2026-10-28 · M2 证据闭环与预算收敛

- 新增 `compassrag/diagnose/verifier.py`：claim 级核验走**独立第二次调用**（答案协议不变，`decisions/D4`）；模型提 claims 与证据编号，**编号由代码校验**（越界/引空/引不存在一律记不支持）；证据为空时不调模型。
- 新增 `compassrag/diagnose/repair.py`：五类失败（query miss / evidence gap / source conflict / answer unsupported / tool failure），前三类规则判定、后两类来自 verifier；`REPAIR_FOR` 一一映射动作，`max_repairs` 用尽一律 REFUSE。
- `agent/core.py`：`answer()` 改成 `控制流 → 核验 → 分类 → 一次补检索修复 → 重答 → 再核验 → （仍不成立）拒答`；`refuse_on_empty_evidence` 打开时无证据**一次模型调用都不发**；补检索查询由未支撑断言原文选出（确定性，不额外调模型）。
- `schemas.py`：新增 `Claim` / `VerificationResult` / `FailureClass` / `RepairAction`，`StopReason` 增加 `insufficient_evidence`，`QuerySource` 增加 `repair`；`AnswerTrace` 记录 verification / failures / repairs。
- `eval/traces.py`：派生行新增 verified / n_claims / unsupported_claims / claim_support_rate / source_conflict / failures / repairs / refused，聚合新增 claim_support_rate、refused_rate、repair_rate。
- 新增 `configs/ablation/plus_verify.yaml`（plus_full + 核验）；默认 `use_verifier=False`，历史行口径不变。
- 测试 155 passed（新增 18）；**再次**用 git 取回第 1 代实现做真实索引等价校验，naive/rewrite/decision 仍逐位一致。
- 未跑任何付费实验。

## 2026-10-28 · M3 脚手架 + M4 故障案例

- M3（未花钱部分）：新增 `compassrag/eval/manifest.py`（配置/模型/语料块数与 id 指纹/抽样指纹/种子/git + `fingerprint()` 可比性判定）、`eval/report.py`（四列表、分层、可比性警告、修复成功率）；新增 `scripts/report_runs.py`（读 runs 出表，默认跳过 `_invalid_`）、`scripts/check_benchmarks.py`（三基准缺口矩阵）；`scripts/eval_qa.py` 增加 `--max-total-tokens / --max-latency-ms / --manifest-dir` 并写 manifest，行里带 `manifest_fp`。
- 修了一个报表坑：旧行没有 `total_tokens`/`ev_full_hit`，聚合原先把缺失算成 0（token/题显示 0、full-hit 显示 0.000）；现在按"有该字段的行"取均值、没有就显示 `-`，并给同基准样本量不一致的行报警。修完后 naive 行 token/题 正确显示 3927，与锚一致。
- M4：写 `docs/failure-cases.md`——三个案例（29% 空答案的生成协议 / 3,815 块批准却跑 20,287 块的超支事故 / wiki 融合负结果），每个按 现象→根因→修复→回归→结果 写，并各带一段 90 秒口述；素材取自 `PROJECT.md` 与 `runs/diag/`，未新跑实验。
- 测试 164 passed（新增报表/manifest 9 条）；M3 改动后再做了一次旧实现逐位等价校验。
- 未跑任何付费实验。

## 2026-10-28 · 计划换版：docs/COMPASSRAG_AGENT_UPGRADE_PLAN.md

- 用户指定新计划 `docs/COMPASSRAG_AGENT_UPGRADE_PLAN.md` 为最新方向（阶段 A–E，含 ResearchTool 对外协议、LangGraph 控制流、FastAPI 服务化与 Atlas adapter）。
- 对照现有实现：A 主体已完成（缺 `ResearchResult`）、B 已完成、D 的 trace/manifest/report 脚手架已完成（缺 plan accuracy / 同预算跑数 / 三基准）、C 与 E 未开始。
- 记 `decisions/D5`：LangGraph 回到主线，取代 `D3`；LangGraph 只管控制流，业务状态仍以 `AgentState`/`AnswerTrace` 为真源。
- 目标（goal）已同步改写为按新计划阶段 A–E 推进。
- 仍有一条后台跑数在用旧控制流（`bash-53bf15ad734f460d`）：在它结束前冻结 `agent`/`diagnose`/`eval` 源码改动，避免污染后几个配置的基线。
- 依赖可行性已验证：`uv pip install --dry-run langgraph` 可解析（阶段 C 不受网络限制）。

## 2026-10-28 · graph 真实数据验证 + plan 弱代理

- 用真实 LLM 跑 naive 2 题，分别走 `--no-graph` 与默认 graph：证据 id / 轮数 / stop_reason / prompt_tokens 全部逐位一致，阶段 C 的等价性从假 LLM 扩到真实通道（约 8k token）。
- 新增 `eval/report.py::plan_route_stats`：用"1 跳→single、≥2 跳→multi"当弱金标算路由一致率，零成本可从已有结果重算；decision/full/verify 三行均为 0.980（n=50）。记 `decisions/D9` 说明它不等于计划质量。
- README 结果区补上这条指标与 caveat；测试 200 passed。
- A–E 盘点入台账：A/B/C/E 完成，D 剩三基准与同预算对照（需付费）。
