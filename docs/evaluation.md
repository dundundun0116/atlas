# 评测方法（Task 6）

本文说明 Atlas 的评测口径与复现方式。**核心纪律：离线脚手架验证与真实模型结果必须分开报告，只有后者能写进简历。**

## 1. 两条评测链路

| 链路 | 入口 | 回答的问题 | 是否能作为简历数字 |
|---|---|---|---|
| 记忆/上下文对照（离线） | `python scripts/run_runtime_eval.py --mode offline` | 策略差异、故障恢复、指标口径是否自洽 | ❌ 仅脚手架验证 |
| 真实模型评测 | `python scripts/run_runtime_eval.py --mode real --model <name> --repeats 3` | 在固定预算与重复次数下，策略对真实模型的影响 | ✅ 需注明样本量/预算/重复次数 |
| MemoryArena 三臂（历史） | `eval/` + `runs/` | 与已有基线（全历史 / 叙事摘要 / 图状态）可比的任务级成绩 | ✅ 已有缩减口径数字，n=2 篇 |

## 2. 任务集（fixture）

三类长程任务，覆盖三种典型失败模式（`atlas/evaluation/fixtures.py`）：

- `multi_hop`：四步累积依赖，后面的子任务需要三步之前学到的事实；
- `conflicting_sources`：同一事实有新旧两版，旧值未标注作废时会被误用；
- `needs_revision`：首轮产出违反约束，必须被打回并修订一次；
- `long_horizon`（可选，`--families long_horizon`）：12 步跨附录召回，用于观察
  "上下文随步数增长"与"结构化记忆收敛"的差异；它**不进**默认对照集，避免短任务均值被拉偏。

fixture 是纯数据且带 `validate()`，因此任务集固定、可版本化。

## 3. 记忆策略

| 策略 | 给模型看什么 | 预期差异 |
|---|---|---|
| `full_history` | 全部历史记录 + 约束 | 不丢信息，但 token 随步数线性增长 |
| `narrative_summary` | 最近两条摘要 | token 最省，但约束/精确值容易丢 |
| `graph_state` | 规则抽取出的事实/约束沉淀成图，四区组装按预算出上下文；已作废旧值不再进入 | 约束与关键值常驻，原始轨迹按预算裁剪 |

## 4. 指标定义

| 指标 | 口径 |
|---|---|
| 完成率 | 整条任务终态是否完成（不是子任务平均） |
| 约束违规率 | 该 run 中出现任意一步违规即计 1（保守口径） |
| 过期信息误用率 | 旧值出现在某一行且该行无"作废/修订"标记，即计 1 |
| 恢复成功率 | 被中断的 run 中恢复并完成的占比；**没有故障注入样本时返回 `null`（表里显示 `—`）**，不再记 1.0，并单列 `interrupted_runs` |
| 上下文 token / 调用次数 / 延迟 / 成本 | token 按实际发送 prompt 计数；离线 harness 的延迟是 `synthetic_estimate`，不代表 API 实测；成本仍是按假定单价估算。`aggregate()` 同时返回 `latency_source` / `cost_source`，表尾会标注估算口径 |

指标定义写在 `atlas/evaluation/metrics.py::REQUIRED_METRICS`，改变口径必须同时改这里。

## 5. 故障注入

`Faults` 支持五类：工具超时（重试一次后失败）、模型空响应（重试一次）、进程中断 + 恢复、重复提交（幂等键命中）、过期决策注入。每条失败路径都有对应测试，确保终态与原因可解释。

## 5.5 长历史缩放（判决实验）

短任务上图状态**不省 token**（计划区与表头开销甚至让它更贵），省 token 只在历史变长后成立。
复现方式：

```python
from atlas.evaluation.fixtures import long_horizon_fixture
from atlas.evaluation import build_policies, run_matrix
for steps in (6, 12, 24, 40):
    runs = {r.policy: r for r in run_matrix([long_horizon_fixture(steps)], build_policies())}
    print(steps, runs["full_history"].context_tokens, runs["graph_state"].context_tokens)
```

跑判决实验（40 步、真 tokenizer、真实模型）：

```bash
set -a; . eval/.env; set +a
export OPENCODE_COMPAT=1 OPENCODE_SESSION=atlas-eval-$(date +%s)
.venv/bin/python scripts/run_runtime_eval.py --mode real \
    --families long_horizon --long-steps 40 --repeats 2 --tokenizer tiktoken
```

口径说明（**报数字时必须一起给出**）：

- "上下文 token"是**整条任务各步实际发送 prompt 的累计值**，不是单次调用；真实模型 prompt 还包含任务目标、约束和当前问题；
- 计数用 `tiktoken/cl100k_base`；`--tokenizer words` 只是廉价代理，不能进简历；
- 成本列 = 累计 token × 假定单价（默认 0.002/千 token），是**估算**，不是账单；
- `temperature=0` 时重复运行结果可能完全一致，重复的作用是排除基础设施抖动，不是降低模型方差。
- 长程 fixture 的事实按子任务逐步提供，不预置未来事实；完成判定要求回答同时包含核心断言和当前问题主体，单独复述数字不算完成。
长任务的计划区按当前问题相关性召回事实；约束和决定始终保留。只有相关性召回无法满足预算时才进入 `PlanZoneOverflow` 降级路径，并通过 `GraphStatePolicy.degraded_contexts` 留痕。

## 6. 回放与排障

`save_run()` 写出 `run.json`（配置 + 每条 run 的逐子任务结果）。`atlas.evaluation.replay` 可以**不调模型**重算指标、按策略/家族分组、并定位到具体子任务：

```python
from atlas.evaluation import replay
report = replay("runs/runtime_eval_real/run.json")
report["metrics"]; report["table"]; report["failures"]
```

## 6.5 确定性长程评测（运行时机制，Phase 5）

上面几节评的是**记忆策略**；这一节评的是**运行时机制**（计划 / 工具调用 / 恢复 / 状态一致性）。
入口：

```bash
python -m atlas.evaluation.longhorizon --tasks 12 --seed 7 --json runs/lh/report.json
# 或
python -m atlas.cli eval --tasks 12 --seed 7 --md runs/lh/report.md
```

它把三件事切成三块，任何一块都不能替另一块说话（这是对"自建 fixture + probe 判分"的回应）：

| 环节 | 实现 | 独立性 |
|---|---|---|
| 出题 | `generate_tasks(count, seed)` | 同一 `(count, seed)` 给出逐字段相同的题集与语料；换种子即换题 |
| 判分 | `AnswerOracle` | 只比对 gold 与干扰项（整数词边界），**不读** agent 的 `verdict`/criteria |
| 注入 | `plan_faults` + `FaultCounter` | 按种子决定第几题注入哪种故障；「计划 / 命中 / 没派发到工具」分开记 |

**题集用虚构实体**（`K-17 型传感器`之类），gold 只出现在一个 chunk 的 `ANSWER:` 标记里。
这一点是必需的：早先版本用真实实体（"爱因斯坦的出生年份"）配随机 gold，脚本化研究者只会抄
所以没暴露，**真模型一跑就露馅**——它按世界知识答 1879，而我们的 gold 是 1914，测出来的
成了"模型肯不肯抄假事实"。换成虚构型号后，答案**只可能**来自检索到的资料。

真模型入口：

```bash
set -a && . eval/.env && set +a          # OPENAI_BASE_URL / OPENAI_API_KEY
python -m atlas.cli eval --tasks 12 --seed 7 --model deepseek-v4.1-flash \
    --json runs/longhorizon/real-12.json
```

真模型跑时报告里的用量标 `measured`（token 与成本都取自响应的 `usage`），
脚本化跑时没有用量段；`reader_kind` 会写进 JSON。

三档判据彼此独立，报告里分开列：

- **任务成功**：oracle 判的，且分两个队列——**干净题**看"能不能做对"，**注入题**看"行为是否符合预期"
  （`timeout` ⇒ 应先对账再续跑然后答对；`invalid_request` ⇒ 应确定性失败，不许重试风暴）；
- **恢复成功**：卡住之后有没有真的跑到终态（不是 runner 的自述）；
- **状态一致**：结束时的结构性不变量——没有停在 `running/verifying` 的 Step、
  没有停在 `dispatched/indeterminate/reconciling` 的调用、**每题工具调用次数 ≤ 1**（未知不重发）。

报告同时写死配置指纹与运行指纹，用来回答"这次跑的是哪一套、和上次是不是同一套"。

**诚实要求**：这里的检索工具与研究者都是脚本化的，测的是**机制**，不是检索质量或模型能力。
样本量 < 30 时报告直接标注「只报机制验证，不报效果数字」，此时**不得**引用成功率为对外结论。
2026-11-04 的实际结果：

| 配置 | 研究者 | 干净题 | 注入题符合预期 | 恢复 | 故障命中 | 工具调用 | 状态一致 | 用量 |
|---|---|---|---|---|---|---|---|---|
| `count=6, seed=7` | 脚本化 | 4/4 | 2/2 | 1/1 | 2/2 | 6（每题 1 次） | 6/6 | — |
| `count=30, seed=11` | 脚本化 | 20/20 | 10/10 | 5/5 | 10/10 | 30（每题 1 次） | 30/30 | — |
| `count=12, seed=7` | `deepseek-v4.1-flash` | 8/8 | 4/4 | 2/2 | 4/4 | 12（每题 1 次） | 12/12 | 30 次调用 / prompt 13,306 tok / completion 12,240 tok / 218s / $0.018 |
| `count=30, seed=11` | `deepseek-v4.1-flash` | **20/20（Wilson 下界 0.839）** | 10/10 | 5/5 | 10/10 | 30（每题 1 次） | 30/30 | 75 次调用 / prompt 33,329 tok / completion 38,284 tok / 612s / $0.054 |

真模型那两行是 **measured**：token 与成本都取自响应的 `usage`，不是按字数估的。
每题约 3 次模型调用、约 1.3k prompt token；30 题那一行过了 30 的门槛，
所以 `mechanism_only=False`，报告里带 Wilson 下界。

**四行里只有最后一行报了统计下界**（脚本化的两行自不必说，研究者是假的；12 题那行 12 < 30）。
但即便 n=30，它们共同的结论也只有一句——
**同一套状态机在注入故障下没有把自己搞坏**：未知结果一次都没重发（每题工具调用恰好 1 次）、
超时那两题走完「对账 → 重开 → 续跑」后答对、确定性失败那两题老老实实失败、12 题全部停在一致状态。

### 真模型跑出来的两个真问题（都已修）

1. **验证步看不到证据**。DAG 里 `verify` 只绑了 `draft`，auditor 手上没有检索结果，
   于是它唯一能说的是"这句话在状态图里找不到出处"——把**正确答案**判成无证据，
   再让修订步把答案删掉。真模型一跑就复现（脚本化 auditor 永远说 ok，所以一直没暴露）。
   修法：`retrieve → verify` / `retrieve → final_verify` 各加一条 data binding，
   `_audit` 把证据块拼进核验者的上下文；有回归用例钉住。
2. **判分口径要提前说清**。模型爱在答案里复述"二手资料中的 X 年不采用"，
   而 oracle 是确定性的——正文里出现干扰值就算错。修法不是放宽 oracle（那会让判分变模糊），
   而是在评测前置说明里明确"最终答案只写结论，不要复述候选数字"。改完 8/8。

第 1 条是真 bug（改的是生产代码），第 2 条是评测契约（改的是喂给模型的说明）——
两者都写进了 `EVAL_PREAMBLE` 的注释与报告口径里，没有偷偷调松判分。

### 公开数据集适配与 DAG 消融（Phase 5 剩余）

**适配层**（`atlas/evaluation/datasets.py`）只做格式映射，**不夹带效果声明**：

```bash
python -c "from atlas.evaluation.datasets import load_jsonl; print(load_jsonl('data/samples/bench_mini.jsonl'))"
```

它按我们自己的 oracle 约束**拒绝**不合形状的记录（gold 必须是数值、语料里只能有一个
`ANSWER:<gold>` 标记、干扰项要显式给），而不是"尽力而为"——评测里最贵的错误是悄悄变松的判分。
接了公开集 ≠ 跑过公开集；这一层到位不等于有可引用数字。

**消融**（`atlas/evaluation/ablation.py`）用**同一批题、同一个判分、同一个脚本化研究者**
跑 `plan_dag_v1` 与 `legacy_research_v1`：

```bash
python -m atlas.evaluation.ablation --tasks 6 --seed 7
```

2026-11-04 实测（6 题，干净题）：两条路径 **6/6 都答对、都到终态、工具调用 6 次、
模型调用 18 次**——干净题上 DAG **没有多花模型调用**（`repair` 在没有 issues 时是纯计算，
不调模型）。差别不在效果而在机制：DAG 有恢复入口、未知结果能对账、下游失效有记录，
`legacy_research_v1` 三样都没有（所以消融表里「恢复入口」一行对它是"没有"，不是 0）。

## 7. 诚实要求

- 离线表格里出现的数字只说明"指标能区分策略"，不说明"Atlas 比摘要强 N 个百分点"；
- 真实评测必须固定：任务集、模型、温度、预算上限、重复次数；
- 任何写进简历的数字都要附上 `run.json` 路径与样本量，且能被 `replay` 复算。
