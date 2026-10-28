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
| 恢复成功率 | 被中断的 run 中恢复并完成的占比；无中断时记 1.0 并单列 `interrupted_runs` |
| 上下文 token / 调用次数 / 延迟 / 成本 | 按 run 汇总后取均值；token 与成本只在运行时真的测量到时才计 |

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

脚本化研究者下这条曲线是确定性的；真实模型只影响"能不能答对"，不影响 token 计数（token 由策略的上下文决定）。
注意 40 步时计划区自身可能超过组装预算，此时走的是**降级为计划区**的路径（与生产服务一致），必须留痕（`GraphStatePolicy.degraded_contexts`）。

## 6. 回放与排障

`save_run()` 写出 `run.json`（配置 + 每条 run 的逐子任务结果）。`atlas.evaluation.replay` 可以**不调模型**重算指标、按策略/家族分组、并定位到具体子任务：

```python
from atlas.evaluation import replay
report = replay("runs/runtime_eval_real/run.json")
report["metrics"]; report["table"]; report["failures"]
```

## 7. 诚实要求

- 离线表格里出现的数字只说明"指标能区分策略"，不说明"Atlas 比摘要强 N 个百分点"；
- 真实评测必须固定：任务集、模型、温度、预算上限、重复次数；
- 任何写进简历的数字都要附上 `run.json` 路径与样本量，且能被 `replay` 复算。
