# CompassRAG 升级状态（2026-10-28）

对应计划：[`COMPASSRAG_AGENT_UPGRADE_PLAN.md`](COMPASSRAG_AGENT_UPGRADE_PLAN.md) 的阶段 A–E。
每条都给**可验证的证据**，而不是"已完成"三个字。HEAD：`ff872eb`。

| 阶段 | 状态 | 证据 |
|---|---|---|
| A 统一状态与工具协议 | ✅ | `agent/schemas.py`（Pydantic 契约）、`agent/tools.py`（六类 outcome，异常不外泄）、`agent/state.py`（四类预算）、`ResearchRequest/Result` + `agent/research.py`；`tests/test_agent_tools.py`（14）、`tests/test_research_result.py`（10）、`tests/test_configs.py`（10） |
| B 证据闭环与有限修复 | ✅ | `diagnose/verifier.py`（claim + **引用编号由代码校验**）、`diagnose/repair.py`（五分类 + 一对一动作 + 上限 + 真 `switch_view`）；`tests/test_diagnosis.py`（21） |
| C LangGraph 控制流 | ✅ | `agent/graph.py`；`use_graph` 默认 True，`eval_qa.py --no-graph` 保留对照；假 LLM 13 条 + **真 LLM 逐题等价抽查**（证据 id / 轮数 / stop / prompt 逐位一致）；`tests/test_agent_graph.py`（13） |
| D 可回放 trace 与报告 | ◐ | `eval/traces.py`（两层落盘）、`manifest.py`（指纹）、`report.py`（四列表 + 分层 + 宽松口径 + 路由弱代理 + token 效率）、全量回放验证、六配置基线、**答案格式实验（H1 确认）**；缺三基准与同预算对照（需付费） |
| E 服务化与 Atlas adapter | ✅ | `api/`（`/research`、`/runs/{id}`、`/health` 含索引版本）、`adapters/atlas.py`（本地 + HTTP）、`scripts/serve_api.py`（`--check` 与 manifest 对账）；`tests/test_research_api.py`（14） |

## 可复现命令

```bash
.venv/bin/python -m pytest -q                      # 228 项，全部不需要真实 API
python scripts/report_runs.py                      # 四列表（含宽松口径、路由弱代理、token 效率）
python scripts/check_benchmarks.py                 # 三基准缺口矩阵
python scripts/serve_api.py --check                # 装配真实索引 + 与 manifest 对账
python scripts/compare_arms.py --base plus_full --arm plus_full_words   # 双臂判据（H1/H2）
```

## 关键实验结论（2026-10-28）

**答案格式实验：H1 成立。** 同一配置只加「≤5 词、不带头衔/括号」约束：
严格 EM **0.460 → 0.600（+14pp）**、F1 0.599 → 0.684、token/题 **11,834 → 11,143（反而更省）**；
逐题 **7 好 0 坏**，残余格式损失 7 → 3 题。本臂 557,165 token。判据预先写死在 `compassrag/eval/arms.py`。

**含义**：此前严格 EM 与宽松 EM 的 0.14–0.22 差距，主要不是模型能力，而是**答案措辞**。
六配置表尚未用新约束重跑，所以公开表里的严格 EM 仍偏低（`decisions/D11`）。

## 已知边界（写出来，不藏）

- **六配置表仍是"旧措辞"口径**：严格 EM 被压低的幅度约 0.14；重跑需 ≈280 万 token。
- **核验层定位取决于口径**：严格 0.420 / 宽松 0.640；加上词数约束后会怎样，C 臂尚未跑（≈100 万 token）。
- **plan accuracy 只有弱代理**（路由 vs 跳数 0.980），没有人工标注的检索计划（`decisions/D9`）。
- **三基准、同预算对照未跑**：2wiki/hotpotqa 缺 samples 语料与实体图。
- **`runs/` 不入库**：数字以本文、README 与 `decisions/` 为准；复现需重跑。
