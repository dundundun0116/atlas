# CompassRAG

> Adaptive Retrieval Decisions over Structured Indexes for Multi-Hop QA

![CompassRAG 架构](docs/assets/architecture.png)

面向多跳问答的 **agentic RAG**：主体是一个以**检索决策**为核心循环的 LLM agent——要不要查、怎么查、查几轮、何时收手。查询改写融合、实体图索引是它逐级点亮的能力组件；公开基准上的**同题集消融阶梯**量化每一项能力的贡献，而不是并列堆砌。

- **检索决策层**：LLM 接管控制流——问题路由（直答 / 单跳 / 多跳）、子问题计划、逐轮检索、证据充分度早停、预算上限。实测 50 题中 37 题提前收手，自适应 1-3 轮。
- **实体图索引**（HippoRAG 式）：逐块用 LLM 抽取实体与关系三元组，构建跨文档实体图（同义边消歧），查询时以查询实体为种子跑 Personalized PageRank——用图上游走单步完成"多跳联想"，作为与 BM25 / 稠密向量**等权互补**的第三路检索信号。
- **查询改写融合**：多跳分解 + HyDE 假答案改写 + 多路 RRF 融合与近重合并。
- **证据闭环**：答案之外再调一次 verifier 把答案拆成 claims，**引用编号由代码校验**（越界/引空/不存在一律判不支持）；失败分五类（query miss / evidence gap / source conflict / unsupported / tool failure），每类一个有限修复动作，修不成就拒答，不硬编。
- **可回放与可服务化**：每题一份 `AnswerTrace`（查询来源、工具 retriever/rank、证据、claims、停止原因、用量），配 manifest 指纹与四列表报表；对外通过 `ResearchTool` 协议（本地 adapter / HTTP `/research`）被上层 Agent 调用。

## 结果一览

**同题集消融阶梯**（MuSiQue dev 分层抽样 50 题；行间严格同题集可比，绝对数字随语料规模变化）：

> **2026-10-28 用新管线（Pydantic 契约 / 工具协议 / 证据闭环 / LangGraph）重测**；行间同题集、同语料、同预算口径，全部 50 题。
> 检索侧与旧版逐位一致（naive 与 +rewrite 的召回、证据完全复现）；EM 与旧值相差 ±2–4 题的原因是**答案表面形式漂移**（如 `10 June 1819` → `June 10, 1819`），不是检索或逻辑退化——判读详见 `decisions/D6`、`D7`。

当前代码已修复决策层的证据覆盖瓶颈：`multi` 路由先检索原问题，再补充子问题，以复用 KG 的原问题实体缓存。离线 50 题对照显示 full-hit 从旧决策路径的 0.24 恢复到 0.60；上表是修复前历史 run，模型端 EM/F1 需重新评测后更新。

| 配置 | EM（严格） | EM（宽松\*\*） | F1 | 证据召回@10 | full-hit@10 | token/题 |
|---|---|---|---|---|---|---|
| naive（单轮固定检索） | 0.300 | 0.440 | 0.432 | 0.720 | 0.460 | 2,162 |
| + 查询改写融合 | 0.400 | 0.560 | 0.575 | 0.720 | 0.460 | 3,540 |
| + 实体图索引 | 0.360 | 0.540 | 0.512 | **0.813** | **0.600** | **2,022** |
| + 检索决策层 | 0.440 | 0.620 | 0.622 | 0.523\* | 0.220\* | 11,935 |
| + 全家桶（三者全开） | 0.460 | 0.600 | 0.599 | 0.522\* | 0.240\* | 11,834 |
| + 证据核验与有限修复 | 0.420 | **0.640** | 0.604 | 0.542\* | 0.280\* | 20,087 |

\* 决策行证据为逐轮累积，@10 只统计第一轮，名义值偏低。
\*\* **宽松 EM 是诊断口径，不是主指标**：金标 token 集合被预测包含即算命中（去括号、忽略日期顺序），用来量化"答案写了 `661 CE` 而金标是 `661`"这类格式损失。它比严格 EM 高 0.14–0.22，说明这套模型普遍答得太"完整"。论文可比口径仍以严格 EM 为准，简历主数字也只写严格 EM。

几个值得单看的数字：

- **决策层的成本叙事**：自适应 1–3 轮（35/50 提前收手），比 naive 高 **+14pp EM**（0.300→0.440），代价是 **5.5× token**。token 倍率比旧版记录的 4.1× 更高，因为新版几乎没有空答案重试，naive 基线本身变便宜了（同一配置 prompt 逐字节一致，token 差 45%——见 D6）。
- **实体图在检索层**：三路等权融合把"两跳证据全捞到"的比例（full-hit）从 0.460 提到 **0.600**，召回率 0.720→**0.813**，而且**最省 token**（2,022/题）。
- **证据核验这一行：严格 EM 看着是负结果，宽松口径下其实是最好的**。严格 EM 0.420（比全家桶少 2 题），但**宽松 EM 0.640 为全表最高**；逐题看，严格口径 +4/−6、宽松口径 +4/−2。也就是说：**核验层确实提高了"实质上答对"的比例，代价是答案更长、被精确匹配扣掉**。claim support 0.885、修复触发 10 次成功 4 次、6 题改成拒答（其中 5 题原本就是错的）。这件事的教训比数字重要：**先排除口径因素，再判一个模块有没有用**（`decisions/D10`）。
- **token 效率（EM 每千 token）**：naive 0.139 · 改写 0.113 · **实体图 0.178（最高）** · 决策 0.037 · 全家桶 0.039 · 核验 0.021。也就是说实体图不仅在准确率和证据覆盖上更好，**还比 naive 更便宜**（2,022 vs 2,162 token/题）；决策层是"花 5.5 倍 token 换 +14pp"，核验层是"花 9 倍换口径存疑的收益"。注意这是**跨配置的效率对照**，不等于"把决策层压到同样预算还能保持 0.44"——真正的同预算对照需要重跑（待预算）。
- **答案格式实验（已跑，H1 确认）**：同一个全家桶配置，只把答案限制成"≤5 词、不带头衔/括号"，严格 EM **0.460 → 0.600（+14pp）**、F1 0.599 → 0.684、**token 反而少 690/题**；逐题 7 好 0 坏。这证明之前严格 EM 与宽松 EM 的 0.14 差距**不是能力问题，是措辞问题**。六配置尚未用新约束重跑（需 ≈280 万 token 预算），所以上表的严格 EM 仍是"旧措辞"口径。
- **路由准确率（弱金标）**：决策三行（decision / full / verify）的"路由 vs 跳数"一致率均为 **0.980**（2-hop 0.96，3/4-hop 1.00，n=50）。MuSiQue 没有人工标注的检索计划，这里用"1 跳应为 single、≥2 跳应为 multi"当弱规则；它只能说明路由没离谱，不等于计划质量（见 `decisions/D9`）。
- **负结果留档**：wiki 式分层摘要索引在加权融合 / 条目先验 / 准入补捞三种修订后仍无净增益，oracle 上限仅 +0.8pp，随后切换到实体图路线。

历史口径（上一代主循环，2026-10-12）：naive 0.300/0.422 · +改写 0.400/0.553 · +实体图 0.400/0.544 · +决策 0.480/0.662 · 全家桶 0.500/0.621。两次测得的检索侧指标一致，差异集中在答案风格。

## 快速开始 Quickstart

```bash
# 1. Setup（Python ≥ 3.10）
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dense]"          # dense extra = 本地 BGE-M3 稠密检索；服务化再加 ".[dense,api]"
cp .env.example .env               # 填入任意 OpenAI 兼容端点：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 2. 验证 LLM 通道
python scripts/smoke_llm.py

# 3. 建语料与索引（musique 抽样题小库：4,646 块；原始数据首次运行自动下载）
python scripts/build_corpus.py --benchmarks musique --from-samples
python scripts/embed_corpus.py --benchmarks musique --variant samples   # 本地向量化（无 API）
python scripts/build_kg.py --extract --variant samples                  # LLM 实体抽取（唯一的大额步骤：约 4.6k 次调用 · ~220 万 token）
python scripts/build_kg.py --finalize --variant samples                 # 本地建图（无 API）
python scripts/ner_queries.py --benchmarks musique --variant samples    # 查询侧 NER 缓存

# 4. 评测
# 检索层（纯本地，零 API）
python scripts/eval_retrieval.py --benchmarks musique --variant samples --modes hybrid,kg,hybrid_kg
# 端到端 QA（configs/ablation/ 下逐级消融开关）
python scripts/eval_qa.py --benchmarks musique --variant samples \
    --config configs/ablation/plus_full_words.yaml --limit 50
```

### 作为 ResearchTool 被调用

```python
from compassrag.agent import Agent, AgentConfig, ResearchRequest
from compassrag.adapters import LocalResearchTool

tool = LocalResearchTool(Agent(llm, tools, chunk_store, AgentConfig(use_decision=True)))
out = tool.research(ResearchRequest(query="...", budget_tokens=4000, max_rounds=2,
                                    idempotency_key="S3-attempt-1"))
# out.answer / out.evidence[*].quote / out.confidence / out.stop_reason / out.trace_id
```

服务化（装配真实索引；`--check` 只体检不监听、不调模型）：

```bash
python scripts/serve_api.py --config configs/ablation/plus_full_words.yaml --check   # 打印索引规模/版本
python scripts/serve_api.py --config configs/ablation/plus_full_words.yaml --port 8000    # POST /research · GET /runs/{trace_id} · GET /health
```

`failure` 是结构化失败信号：`no_evidence` / `budget_exhausted` / `upstream_error` / `timeout` / `invalid_request`；
模型回 `unknown` 也算 `no_evidence`（有证据但没答出来），此时 `confidence=0`。

失败也是结构化结果：`no_evidence` / `budget_exhausted` / `upstream_error` / `timeout` / `invalid_request`。

复现实验小贴士：所有批量脚本都带**断点续跑**（按 id / 块 id 增量落盘，中断后原命令重跑即可）；`--variant samples` 表示"抽样题小库"，不带则用全量 dev 语料库；`configs/ablation/` 每个文件就是一个消融级别。

## 评测

口径、五组配置、指标定义与复现命令见 [docs/evaluation.md](docs/evaluation.md)。
正式评测固定题集/索引/模型/预算并跑 ≥2 次重复，汇总在 `runs/eval/summary.json`。

```bash
python scripts/run_resume_eval.py --repeats 2 --parallel 3 --strict-answer-words 5
python scripts/report_runs.py
```

## 一条命令起演示

```bash
bash scripts/demo.sh              # 用 data/cache 的基准索引起前后端（Ctrl-C 一起退出）
bash scripts/demo.sh --ingest     # 用 runs/ingest 里最新构建的上传文档索引
# 或者容器化（需 Docker Desktop 运行）
docker compose up --build
```

架构、端口、状态机与失败语义、索引版本固定见 [docs/fullstack-architecture.md](docs/fullstack-architecture.md)。

## 研究工作台（Web）

前端是 Vite + React + TypeScript，类型由后端 OpenAPI 生成，**组件里不复制业务 schema**：

```bash
# 1. 后端（装配真实索引；--check 只体检）
python scripts/serve_api.py --config configs/ablation/plus_full_words.yaml --port 8000

# 2. 类型（后端改了契约就重新生成）
python scripts/export_openapi.py
cd web && npm install && npx openapi-typescript openapi.json -o src/api/types.ts

# 3. 前端
npm run dev          # http://localhost:5173（/research 等路径代理到 8000）
npm test             # 组件测试
npm run build
```

语料与索引（Task 4）：`POST /documents` 上传 txt/md/json（指纹去重），`POST /index/build` 生成新版本，
`GET /index/status` 同时给 `runtime_version`（服务启动时装的）与 `built_version`（最新构建）——
两者不一致会显式告警，因为那意味着"你看到的数字和跑出来的不是同一版索引"。

界面包含五块：提问与预算配置、答案（置信度/停止原因/拒答）、证据（来源/检索器/排名/引用片段）、过程时间线（SSE 实时推送 `plan → retrieve → verify → answer → finish`）与历史运行（打开既有 trace，**不重新调用模型**）。
异步运行用 `POST /research/async` 拿 `run_id`，再订阅 `GET /runs/{id}/events`；`POST /runs/{id}/cancel` 可取消，取消后不再发起新的模型或检索调用。

## 项目结构

```
compassrag/
├── compassrag/
│   ├── agent/            # 控制流与契约
│   │   ├── schemas.py    #   Pydantic：Route/SearchRequest/ToolCall/Claim/AnswerTrace/ResearchResult
│   │   ├── tools.py      #   统一工具协议（六类 outcome，异常不外泄）
│   │   ├── state.py      #   运行状态与四类预算（轮数 / token / 延迟 / 费用）
│   │   ├── core.py       #   顺序实现：plan → retrieve* → verify* → answer（对照基线）
│   │   ├── graph.py      #   LangGraph 控制流（与 core 由对照测试锁死）
│   │   └── research.py   #   ResearchResult 适配层
│   ├── diagnose/         # 证据闭环：claim 核验（verifier）+ 五类失败与有限修复（repair）
│   ├── api/              # 阶段 E：FastAPI 三端点 + trace 存储
│   ├── adapters/         # 阶段 E：Atlas 本地 / HTTP adapter（同一 ResearchTool 协议）
│   ├── decision/         # 决策层：路由与计划（planner）+ 充分度早停（sufficiency）
│   ├── query/            # 查询侧：多跳分解 + HyDE 改写（带来源标注）
│   ├── index/            # 索引：实体关系图（kg.py，含 PPR）+ wiki 摘要索引（负结果留档）
│   ├── retrieval/        # BM25 / BGE-M3 稠密 / RRF 融合与近重合并
│   ├── corpus/           # 基准接入、分层抽样、分块
│   ├── eval/             # 指标 / 断点续跑 / traces（轨迹+派生行两层）/ manifest / report
│   └── llm/              # openai 兼容客户端 + 逐调用 token 遥测（IPv4 出口与离线模型说明见 .env.example）
├── configs/
│   ├── models.yaml       # 端点与请求参数
│   └── ablation/         # naive / plus_rewrite / plus_kg / plus_decision / plus_full
├── scripts/              # 语料构建 / 向量化 / 建图 / NER / 检索评测 / QA 评测 / 报表 / 基准缺口检查
├── data/samples/         # 三基准 300 题分层抽样清单（seed=42，入仓可复现）
├── docs/assets/          # 架构图（.pptx 可编辑源 + 渲染 PNG）
└── tests/                # 单测套件（218 项，全部无需真实 API）
```

## 现状与路线图

- ✅ 已完成：三基准接入与分层抽样；BM25 + BGE-M3 混合检索；实体图索引（建图 / PPR 检索 / 接入 agent）；查询改写融合；检索决策层（路由 / 计划 / 早停 / 预算）；**统一工具协议与 Pydantic 契约**；**证据闭环（claim 核验 + 五类失败 + 有限修复 + 拒答）**；**逐题 trace / manifest / 回放 / 四列表报表**；**LangGraph 控制流（与顺序实现对照等价）**；**ResearchTool 服务化与 Atlas adapter**。
- 🚧 进行中：六配置在新管线上的重跑（含 `plus_verify` 行）与同预算对照；README 与文档打磨。
- 📌 路线图：MultiHop-RAG（含 null query 的拒答检测）与 FRAMES（三轴 LLM-judge）基准接入；三基准正式结果（当前仅 MuSiQue）；CLI / 交互 demo；扩题到 100 题。

**已知局限**：消融数字来自单一基准（MuSiQue）的 50 题子集；实体抽取依赖 LLM（建索引成本随语料线性增长）；决策行的证据召回按逐轮累积计量，与单轮行的 @k 口径不完全对齐（表中已标注）。

## 方法参照

本项目的机制设计受益于以下工作，均只借鉴思路、未复用代码：

- **实体图 + PageRank 检索**：[HippoRAG](https://arxiv.org/abs/2405.14831)（NeurIPS 2024）/ [HippoRAG 2](https://arxiv.org/abs/2502.14802)（ICML 2025）——本项目的实体图索引即此形态的独立实现（含关闭思维链的抽取协议与同义边消歧细节）。
- **索引层设计空间**：[SiReRAG](https://arxiv.org/abs/2412.06206)（ICLR 2025，"相似性 vs 关联性"二分）、[RAPTOR](https://arxiv.org/abs/2401.18059)（ICLR 2024，递归摘要树）。
- **规模与方法对照**：[BM25 Wins at Scale](https://arxiv.org/abs/2607.26497)、[RAG vs. GraphRAG 系统评测](https://arxiv.org/abs/2502.11371)。
- **查询侧**：HyDE / RAG-Fusion / [Adaptive-RAG](https://arxiv.org/abs/2403.14403)（NAACL 2024，难度路由参照）。

基准数据：[MuSiQue](https://github.com/StonyBrookNLP/musique)、HotpotQA、2WikiMultiHopQA（均使用官方 dev 划分，distractor 设定）。

## License

[MIT](LICENSE)
