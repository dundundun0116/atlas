# CompassRAG 项目全貌与状态（供外部审查）

> 本文档写给**不了解这个项目的审查者**：它描述项目在做什么、当前做到哪、每个数字从哪来、
> 哪些结论站得住、哪些还没有证据。文末附「可质询清单」——欢迎直接指着条目问。
>
> 生成时间：2026-10-28。代码基线：`git log` 见 §10。

---

## 1. 一句话与定位

**CompassRAG 是一个面向多跳问答的「检索决策 Agent」**：它决定是否检索、拆几个子问题、每轮查什么、
什么时候停、答案是否被证据支持、不支持时补查还是拒答。它只负责**单个问题的证据获取**，
不承担跨任务长期状态（那是兄弟项目 Atlas 的职责）。

对外它按 `ResearchTool` 协议被调用：输入问题 + 预算 + 幂等键，输出**答案 + 结构化证据 +
置信度 + 停止原因 + trace 指针**，失败也返回结构化类别而不是异常。

## 2. 架构与数据流

```
浏览器工作台 (Vite+React+TS, :5173)
      │  HTTP / SSE
      ▼
FastAPI (:8000)  ── /research · /research/async · /runs/{id}/events · /runs/{id}/cancel
      │                     /documents · /index/* · /health · /sessions/{id}
      ▼
ResearchTool 适配层（agent/research.py）
      ▼
LangGraph 控制流（agent/graph.py）  plan → retrieve* → verify → prep → answer → (repair → answer)* → finish
      │                              ↑ 条件边：direct / single / multi / sufficient / budget / repair
      ├── 工具注册表（agent/tools.py）：BM25 / BGE-M3 稠密 / 实体图 PPR，统一 outcome
      ├── 证据闭环（diagnose/）：claim 核验（引用编号由代码校验）+ 五类失败 + 有限修复 + 拒答
      ├── 状态与预算（agent/state.py）：轮数 / token / 延迟 / 费用四类上限
      └── 轨迹（AnswerTrace）：查询来源、工具调用、证据、停止原因、用量；SSE 是它的实时投影
```

**两条请求路径**：同步 `/research/sync` 等结果；异步 `/research/async` 立刻拿 `run_id`，
用 SSE 看进度、用 `GET /runs/{id}` 取 `summary + result + trace`、`POST /runs/{id}/cancel` 取消。
**状态机**：`queued → running → completed | failed | cancelled`。

## 3. 代码地图

| 路径 | 职责 |
|---|---|
| `compassrag/agent/schemas.py` | Pydantic 契约：Route / SearchRequest / ToolHit / ToolCall / Claim / VerificationResult / StopReason / AnswerTrace / ResearchRequest / ResearchResult |
| `compassrag/agent/tools.py` | 统一工具协议，六类 outcome（ok/empty/error/timeout/invalid_params/index_missing），异常不外泄 |
| `compassrag/agent/state.py` | 运行状态、四类预算、事件投影、协作式取消 |
| `compassrag/agent/core.py` | **顺序实现**（对照基线）：plan → retrieve* → answer + 核验/修复 |
| `compassrag/agent/graph.py` | **LangGraph 实现**（默认），与 core 由对照测试锁死 |
| `compassrag/agent/research.py` | ResearchResult 适配：证据片段、失败分类、置信度（启发式） |
| `compassrag/diagnose/verifier.py` | claim 拆分 + **引用编号的确定性校验** |
| `compassrag/diagnose/repair.py` | 五类失败分类、一对一修复动作、上限、`switch_view` 只走一路检索 |
| `compassrag/eval/{traces,manifest,report,arms,metrics}.py` | 轨迹两层落盘 / run 指纹 / 四列表 / 双臂判据 / 指标 |
| `compassrag/api/*` | 契约、运行注册表、事件总线、SSE、上传与索引版本 |
| `compassrag/adapters/atlas.py` | 本地 adapter 与 HTTP adapter（同一协议） |
| `web/` | 工作台：提问/答案/证据/时间线/历史/语料面板；类型由 OpenAPI 生成 |

## 4. 当前完成度

### 4.1 计划 A（`docs/COMPASSRAG_AGENT_UPGRADE_PLAN.md` 阶段 A–E）

| 阶段 | 状态 | 证据 |
|---|---|---|
| A 统一状态与工具协议（含 ResearchResult） | ✅ | 契约与工具测试 24 条 |
| B 证据闭环与有限修复 | ✅ | 诊断测试 21 条（含 switch_view 端到端） |
| C LangGraph 控制流 | ✅ | 假 LLM 13 条 + **真 LLM 逐题等价抽查**（证据 id/轮数/stop/prompt 逐位一致） |
| D 可回放 trace 与评测报告 | ◐ | 轨迹两层落盘、manifest 指纹、四列表、全量回放验证；**缺三基准与同预算对照** |
| E 服务化与 Atlas adapter | ✅ | API 14 条 + 真实索引零成本冒烟 + `serve_api --check` 与 manifest 对账 |

### 4.2 计划 B（`/Users/szx/atlas/docs/superpowers/plans/2026-10-28-compassrag-fullstack-plan.md` Task 1–6）

| Task | 状态 | 备注 |
|---|---|---|
| 1 产品契约 | ✅ | ResearchSession/RunSummary/TraceEvent/五状态机/错误映射 |
| 2 可观察运行流 | ✅ | 节点事件、SSE、取消、异步超时 |
| 3 React 工作台 | ✅ | 11 条组件测试；真实浏览器跑通（含 direct 与多轮两条路径） |
| 4 文档与索引版本 | ✅ | 上传/去重/构建/版本固定/状态；6 条测试 |
| 5 一条命令与部署 | ✅ | `scripts/demo.sh`（实测 200/200）、Dockerfile×2、compose（未实机构建）、架构文档 |
| 6 正式评测与简历证据 | 🚧 | 脚本与口径文档已完成；**2 次重复的正式跑数进行中** |

## 5. 评测：口径与数字

### 5.1 口径纪律（先看这条，否则数字会被误读）

- **严格 EM**（SQuAD 精确匹配）是对外主指标；**宽松 EM**（金标 token 集合被预测包含）只作诊断，
  **不进简历**。两者差距 0.14–0.22，来源是"答案写得过于完整"（见 §5.3 实验）。
- 每次运行都写 manifest：配置、模型、语料块数与指纹、抽样哈希、种子、git commit。
  指纹不同的 run 不可直接比较。
- `token/题` 会随模型行为漂移（重试率变化），**跨时间只比 EM/F1 与证据指标**。
- 历史表与正式评测表分属两套目录（`runs/qa/` 与 `runs/eval/`），不可混算。

### 5.2 历史基线（无答案词数约束，n=50，2026-10-28）

| 配置 | 严格 EM | 宽松 EM | F1 | 召回@10 | full-hit | token/题 |
|---|---|---|---|---|---|---|
| naive | 0.300 | 0.440 | 0.432 | 0.720 | 0.460 | 2,162 |
| +rewrite | 0.400 | 0.560 | 0.575 | 0.720 | 0.460 | 3,540 |
| +kg | 0.360 | 0.540 | 0.512 | **0.813** | **0.600** | **2,022** |
| +decision | 0.440 | 0.620 | 0.622 | 0.523* | 0.220* | 11,935 |
| +full | 0.460 | 0.600 | 0.599 | 0.522* | 0.240* | 11,834 |
| +verify | 0.420 | **0.640** | 0.604 | 0.542* | 0.280* | 20,087 |

\* 决策行证据逐轮累积，@10 只算第一轮，名义值偏低。数据：`runs/qa/musique__*__samples.jsonl`。

### 5.3 关键实验：答案格式（`decisions/D11`）

同一配置（plus_full）只加「≤5 词、不带头衔/括号」约束：

| 臂 | 严格 EM | 宽松 EM | F1 | token/题 |
|---|---|---|---|---|
| A 无约束 | 0.460 | 0.600 | 0.599 | 11,834 |
| B 有约束 | **0.600** | **0.660** | **0.684** | **11,143** |

逐题 **7 好 0 坏**，残余格式损失 7→3 题，成本 557,165 token。判据预先写死在
`compassrag/eval/arms.py`（严格 ≥ +0.08 且宽松不降 → H1）。结论：**那 0.14 是措辞问题，不是能力问题**。

### 5.4 正式评测（加词数约束，2 次重复）

**第 1 次重复（n=50，已完成）**：

| 配置 | 严格 EM | 宽松 EM | F1 | 召回 | token/题 | 轮数 |
|---|---|---|---|---|---|---|
| naive | 0.400 | 0.460 | 0.471 | 0.720 | 2,285 | 1.00 |
| +rewrite | 0.560 | 0.620 | 0.644 | 0.720 | 3,605 | 1.00 |
| +kg | 0.480 | 0.560 | 0.591 | 0.813 | 1,927 | 1.00 |
| +decision | 0.540 | 0.640 | 0.672 | 0.497 | 12,574 | 2.22 |
| +verify | 0.520 | 0.620 | 0.630 | 0.522 | 21,221 | 2.18 |

可以看到加约束后**每个配置都比无约束口径高 0.10–0.16**，与 §5.3 的单配置实验一致；
同时 `naive` 从 0.300 → **0.400**，说明"措辞压低"是全局现象，不是某一个配置的偶然。

**第 2 次重复：进行中**（截至本文生成：naive/kg 已满 50，rewrite 49/50，decision 45/50，verify 21/50）。
汇总文件 `runs/eval/summary.json`（均值±标准差）在两次重复都完成后生成。**因此 §5.4 目前只有单次数字，
不具备波动区间，不能作为最终结论。**

### 5.5 成本与其它零散结论

- 六配置基线一轮：1,212 次调用 / **2,795,042 token**；答案格式实验：557,165 token；正式评测预计 400–500 万。
- **token 效率（EM/千 token，无约束口径）**：kg 0.178（最高，比 naive 还省）、naive 0.139、
  rewrite 0.113、decision 0.037、full 0.039、verify 0.021。注意这是跨配置效率对照，不是同预算对照。
- **路由-跳数一致率（弱金标）**：decision/full/verify 均 0.980（n=50）。它只说明"路由没离谱"，
  **不等于计划质量**（没有人工标注的检索计划）。

## 6. 关键决策（每条都有卡片，`decisions/D<n>.md`）

| 编号 | 一句话 |
|---|---|
| D1 | naive 行作不可变锚点（后经 D6 修订口径） |
| D2 | 结构化输出用「预算阶梯 + 解析失败安全降级」，不用 function calling |
| D3 → D5 | 先暂缓 LangGraph，后因新计划要求回主线（D3 被 D5 取代） |
| D4 | 证据核验走独立第二次调用，不改编答案生成协议 |
| D6 | 锚点改为 EM/F1 + 逐题 prompt；token 只记录不设阈值（重试率漂移） |
| D7 | 答案风格会漂移：EM/F1 只能作容差判据 |
| D8 | `use_graph` 默认 True，顺序实现保留作对照 |
| D9 | plan accuracy 用「路由 vs 跳数」弱代理，不等于计划质量 |
| D10 | 严格 EM 为主、宽松 EM 为诊断；否定结论先排除口径 |
| D11 | 答案格式实验：H1 成立，词数约束应成为默认 |

教训卡（`lessons/`）：清理了 6 条真实踩坑（空答案归因、包遮蔽模块、作废口径混入聚合、模型已缓存仍联网、Teredo IPv6 卡死、脚本函数定义位置）与 3 条机制经验。

## 7. 已知边界与未完成项（审查时请重点看这里）

1. **只有 MuSiQue 一个基准**：2wiki/hotpotqa 缺小库语料与实体图，三基准未跑。
2. **没有同预算对照**：所有"决策层更准"的结论都伴随 5–6 倍 token，**因果结论未建立**。
3. **正式评测第 2 次重复未完成**，§5.4 只有单次数字，无波动区间。
4. **plan accuracy 无真金标**，只有弱代理。
5. **上传文档索引只有 BM25**（没有向量/实体图），`--index-source ingest` 是单路检索。
6. **Docker 镜像未实机构建**（本机 daemon 未启动，只做了 `compose config` 校验）。
7. **Playwright 未固化**：浏览器端到端是手工验证 + 组件测试。
8. **`runs/` 不入库**：数字以文档与 `decisions/` 为准，复现需重跑。
9. 答案格式仍有残余损失（3/50 题）。

## 8. 复现命令

```bash
.venv/bin/python -m pytest -q                                  # 后端 252 项，全部无需真实 API
cd web && npm install && npm test && npm run build              # 前端 11 项 + 构建

python scripts/run_resume_eval.py --repeats 2 --parallel 3 --strict-answer-words 5   # 正式评测
python scripts/report_runs.py                                   # 四列表
python scripts/compare_arms.py --base plus_full --arm plus_full_words                 # 双臂判据
python scripts/check_benchmarks.py                              # 三基准缺口矩阵
python scripts/serve_api.py --check                             # 装配索引 + 与 manifest 对账
bash scripts/demo.sh                                            # 一条命令起服务
```

## 9. 证据地图（claim → 出处）

| 说法 | 证据 |
|---|---|
| 工具异常不再中断整轮评测 | `agent/tools.py` 六类 outcome + `tests/test_agent_tools.py` |
| 引用编号由代码校验 | `diagnose/verifier.py::resolve_claims` + `tests/test_diagnosis.py` |
| LangGraph 与顺序实现等价 | `tests/test_agent_graph.py`（13 条）+ 真 LLM 抽查记录（台账 2026-10-28） |
| 轨迹可离线回放 | 300 条 trace 零解析失败；naive 50/50 重算与落盘一致（台账记录） |
| SSE 事件流与取消 | `tests/test_research_api.py`（21 条） |
| 上传文档闭环 | `tests/test_e2e_ingest_flow.py`（零 API 费用） |
| 严格/宽松口径差 | `decisions/D10` + `eval/metrics.py::relaxed_em` |
| 答案格式实验 H1 | `docs/experiment-answer-format.md` + `eval/arms.py` |

## 10. 变更记录（git）

`git log --oneline -12` 依次为：A–E 升级、台账、双臂工具、实验结论、STATUS、工作台、E2E 修复、
ingest 后端、语料面板、部署、评测脚本。完整哈希见仓库历史。

## 11. 可质询清单（欢迎直接问）

1. §5.2 与 §5.4 数字口径不同（无约束 vs 有约束），是否在文档中处处标明？
2. §5.4 只有 1 次重复，波动未知——**能否先把第 2 次跑完再引用**？
3. "决策层 +14pp" 的代价是 5.5× token，**同预算下还能不能保持**？没有实验。
4. verify 行严格 EM 低于 full、宽松 EM 最高——这个结论是否被过度解读？
5. 上传文档只有 BM25，`/health` 里的 `index` 是否足以让调用方知道"这不是三路检索"？
6. 失败分类里 `no_evidence` 同时覆盖"没有证据"和"有证据但没答出"，对上游是否够用？
7. 事件是轨迹的投影，SSE 断开不影响执行——断开时的语义在契约里写清了吗？
8. 所有测试都用假 LLM，**真实模型的回归保障**只有抽查，够不够？
