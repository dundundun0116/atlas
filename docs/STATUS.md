# CompassRAG 升级状态（2026-10-28）

对应计划：[`COMPASSRAG_AGENT_UPGRADE_PLAN.md`](COMPASSRAG_AGENT_UPGRADE_PLAN.md) 的阶段 A–E。
每条都给**可验证的证据**，而不是"已完成"三个字。

| 阶段 | 状态 | 证据 |
|---|---|---|
| A 统一状态与工具协议 | ✅ | `agent/schemas.py`（Pydantic 契约）、`agent/tools.py`（六类 outcome，异常不外泄）、`agent/state.py`（四类预算）、`ResearchRequest/Result` + `agent/research.py`；`tests/test_agent_tools.py`（14）、`tests/test_research_result.py`（10）、`tests/test_configs.py`（10） |
| B 证据闭环与有限修复 | ✅ | `diagnose/verifier.py`（claim + 代码校验引用）、`diagnose/repair.py`（五分类 + 一对一动作 + 上限）、`strict_answer_words` 前的答案是独立第二次调用（`decisions/D4`）；`tests/test_diagnosis.py`（21） |
| C LangGraph 控制流 | ✅ | `agent/graph.py`；`use_graph` 默认 True，`eval_qa.py --no-graph` 保留对照；假 LLM 12 条 + **真 LLM 逐题等价抽查**（证据 id / 轮数 / stop / prompt 逐位一致）；`tests/test_agent_graph.py`（13） |
| D 可回放 trace 与报告 | ◐ | `eval/traces.py`（两层落盘）、`manifest.py`（指纹可比性）、`report.py`（四列表 + 分层 + 宽松口径 + 路由弱代理）、六配置重跑结果；**三基准与同预算对照未做（需付费）** |
| E 服务化与 Atlas adapter | ✅ | `api/`（`POST /research`、`GET /runs/{id}`、`GET /health` 含索引版本）、`adapters/atlas.py`（本地 + HTTP，同一 `ResearchTool` 协议）、`scripts/serve_api.py`；`tests/test_research_api.py`（14） |

## 可复现命令

```bash
.venv/bin/python -m pytest -q                      # 218 项，全部不需要真实 API
python scripts/report_runs.py                      # 四列表（含宽松口径与 plan 弱代理）
python scripts/check_benchmarks.py                 # 三基准缺口矩阵
python scripts/serve_api.py --port 8000            # 起服务
```

## 已知边界（写出来，不藏）

- **严格 EM 与宽松 EM 差 0.14–0.22**：模型答得太"完整"（`661` → `661 CE`）。严格 EM 是对外口径，宽松只作诊断（`decisions/D10`）。
- **核验层在严格口径下不占优**（0.420 vs 0.460），宽松口径下最好（0.640 vs 0.600）。结论取决于口径，报告必须两列并列。
- **plan accuracy 只有弱代理**（路由 vs 跳数，0.980），没有人工标注的检索计划（`decisions/D9`）。
- **三基准、同预算对照未跑**：2wiki/hotpotqa 缺 samples 语料与实体图，需付费。
- 答案格式实验（`docs/experiment-answer-format.md`）已设计并预注册判据，开关已实现，**未跑**。
