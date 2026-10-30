# CompassRAG 检索决策 Agent 升级计划

## 定位

CompassRAG 的最终定位是一个可被其他 Agent 调用的检索决策 Agent。它负责把一个研究问题查清楚：判断是否需要检索、选择检索路径、生成查询、调用多个检索工具、判断证据是否充分，并在预算内停止、补查或拒答。

Atlas 是它的上层长程任务运行时。Atlas 管任务拆解、跨子任务状态、角色协作和恢复；CompassRAG 管单个子问题的证据获取与检索决策。CompassRAG 不承担长期记忆，也不复制 Atlas 的任务状态图。

## 最终目标架构

```text
ResearchTool.request
  |
  v
Route / Plan
  |
  v
LangGraph: plan -> retrieve -> verify -> repair? -> answer
  |                 |
  |                 +-- evidence sufficiency / claim support
  +-------------------- tool budget / latency / cost guard
  |
  v
Tool Registry
  +-- BM25S
  +-- BGE-M3 dense
  +-- Entity Graph + PPR
  |
  v
RRF + dedup + provenance
  |
  v
ResearchResult: answer + evidence + confidence + stop reason + trace
```

LangGraph 只负责检索 Agent 的控制流；检索工具、证据、来源和评测 trace 使用 CompassRAG 自己的 schema。

## 最终简历版本

> **CompassRAG——面向多跳问答的检索决策型 Agent｜独立开发**  
> **技术栈：** Python、OpenAI SDK、Pydantic、LangGraph、BM25S、Sentence Transformers/BGE-M3、实体关系图检索、RRF、Structured Agent Tracing
>
> 针对传统 RAG 固定“检索一次再生成”导致多跳证据缺失、无效检索浪费 token、证据不足却强行回答的问题，设计并实现一个以检索决策为核心控制流的 Agent。系统根据问题难度选择直答、单跳或多跳路径，动态生成检索计划，调用 BM25、稠密向量和实体关系图三类工具获取证据，并在每轮后判断证据是否充分，支持早停、追加查询、有限修复和拒答。
>
> 使用 Pydantic 约束路由、查询计划、工具参数、证据判断和最终响应；通过统一 Tool Registry 封装不同检索器的输入、输出、超时、空结果和索引错误，模型输出异常时降级为安全的单轮检索。
>
> 融合 BM25、BGE-M3 和实体关系图 PPR 检索信号，使用 RRF 和近重去重形成候选证据集；通过多跳查询分解、HyDE 和证据充分度判断补齐第二跳信息，并为每个结果保存检索器、排名、查询来源和原文片段。
>
> 增加证据闭环：答案被拆成带引用的 claims，系统检查每条 claim 是否有真实证据支持；根据 query miss、evidence gap、source conflict、unsupported answer 和 tool failure 选择补检索、改写、切换视图或拒答，所有循环受 token、轮数、延迟和费用上限约束。
>
> 基于 MuSiQue、2WikiMultiHopQA 和 HotpotQA 的固定子集进行同题集消融，比较固定单轮 RAG、查询改写、实体图索引和自适应决策；统计 EM/F1、证据 full-hit、claim support rate、拒答准确率、平均检索轮数、提前停止率、token、延迟和单位问题成本，并保存逐题 trace 支持 replay。

## 对外接口：ResearchTool

Atlas 通过这个协议调用 CompassRAG。协议必须与内部检索器解耦。

### 请求

```json
{
  "session_id": "session-001",
  "subtask_id": "S3",
  "query": "需要回答的问题",
  "budget_tokens": 4000,
  "max_rounds": 2,
  "idempotency_key": "S3-attempt-1"
}
```

### 响应

```json
{
  "answer": "阶段性答案",
  "evidence": [
    {
      "source": "article-title",
      "chunk_id": "chunk-001",
      "quote": "支持答案的原文片段",
      "score": 0.91,
      "retriever": "kg",
      "rank": 2
    }
  ],
  "confidence": 0.84,
  "retrieval_rounds": 2,
  "stop_reason": "evidence_sufficient",
  "trace_id": "trace-001"
}
```

失败也必须返回结构化结果：`timeout`、`budget_exhausted`、`no_evidence`、`upstream_error` 或 `invalid_request`。Atlas 可以据此决定重试、暂停或修改子任务。

## 分阶段实施

### 阶段 A：统一 Agent 状态和工具协议

**目标：** 让当前脚本式循环变成可解释的工具调用 Agent，并稳定历史消融结果。

**文件：**

- `compassrag/agent/schemas.py`
- `compassrag/agent/state.py`
- `compassrag/agent/tools.py`
- `compassrag/agent/core.py`
- `tests/test_agent_tools.py`
- `tests/test_agent.py`

**任务：**

- [ ] 定义 `RouteDecision`、`SearchPlan`、`SearchRequest`、`ToolHit`、`ToolCall`、`EvidenceCheck`、`StopReason`、`AnswerTrace` 和 `ResearchResult`。
- [ ] 将 BM25、dense、KG 和可选 wiki 检索器注册到统一 Tool Registry。
- [ ] 每次检索记录 query、query_source、retriever、rank、score、chunk_id、latency 和 outcome。
- [ ] 统一 scripted 与 decision 两条路径，使用同一份 evidence 累积、截断、答案和 token 聚合逻辑。
- [ ] planner/sufficiency 继续保留预算阶梯和安全降级，但最终结果必须经过 Pydantic schema 校验。
- [ ] 工具失败分类为 timeout、empty、index_missing、invalid_params 和 error，不把异常直接抛出到评测入口。
- [ ] 首次重构必须证明 naive 行仍复现原来的 EM/F1/token 基线。

**验收：** 模型格式错误时安全降级；单个检索器失败不影响其他工具；一条答案可以完整列出计划、查询、工具、证据和停止原因；历史消融行为保持一致。

### 阶段 B：证据闭环和有限修复

**目标：** 让 Agent 能判断答案是否真的由证据支持，而不是检索到内容就生成。

**文件：**

- `compassrag/diagnose/verifier.py`
- `compassrag/diagnose/repair.py`
- `tests/test_diagnosis.py`
- `compassrag/agent/core.py`

**任务：**

- [ ] 保持原答案生成协议不变，额外调用 verifier 生成 claims，避免破坏历史 EM/F1 口径。
- [ ] 代码检查 claim 引用是否越界、是否引用真实 chunk、是否与证据集合一致。
- [ ] 将失败分为 query miss、evidence gap、source conflict、unsupported answer 和 tool failure。
- [ ] 为每类失败定义有限动作：改写查询、补检索、切换 KG/dense 视图、拆分子问题或拒答。
- [ ] 设置最大轮数、最大 token、最大延迟和最大费用；达到上限返回 `budget_exhausted`。
- [ ] 证据为空或 null query 时默认拒答，不允许模型凭空生成事实。

**验收：** 每条 claim 都能定位支持来源或明确标记 unsupported；修复不会无限循环；空证据、矛盾证据和工具失败不会误判为成功。

### 阶段 C：LangGraph 控制流

**目标：** 让检索循环具有可视化、可测试和可恢复的控制流边界。

**文件：**

- `compassrag/agent/graph.py`
- `compassrag/agent/state.py`
- `tests/test_agent_graph.py`
- `pyproject.toml`

**任务：**

- [ ] 定义 `plan`、`retrieve`、`verify`、`repair`、`answer` 和 `finish` 节点。
- [ ] 用条件边表达 direct、single、multi、sufficient、repair_required 和 budget_exhausted。
- [ ] LangGraph state 只保存 schema 化的控制状态；证据 provenance 和指标由项目对象负责。
- [ ] 让本地 CLI、批量评测和 Atlas adapter 共用同一 graph runner。
- [ ] 保留 scripted 配置作为固定管线对照，不把它从评测中删除。

**验收：** 同一个 graph runner 支持单题调用、批量评测和 Atlas ResearchTool；每个终止节点都有明确 stop reason；控制流测试不需要真实 API。

### 阶段 D：可回放 trace 和评测报告

**目标：** 把已有消融表升级为 Agent 过程评测，而不是只报告答案分数。

**文件：**

- `compassrag/eval/traces.py`
- `compassrag/eval/replay.py`
- `compassrag/eval/metrics.py`
- `tests/test_trace_replay.py`
- `scripts/eval_qa.py`
- `scripts/report_runs.py`

**任务：**

- [ ] 每个 run 保存配置快照、模型、语料版本、随机种子、完整查询列表、工具调用、证据 ids、claims、停止原因和错误。
- [ ] 增加 plan accuracy、tool success rate、evidence full-hit、claim support rate、拒答准确率、平均轮数、提前停止率和修复成功率。
- [ ] 对 naive、rewrite、KG、decision 和 full 保持同题集消融。
- [ ] 增加同 token/延迟预算对照，避免把“多花 4 倍 token”误写成无成本提升。
- [ ] 按 1/2/3/4-hop 分层统计，报告均值和样本量。
- [ ] replay 不重新调用模型即可重算证据、停止和聚合指标。

**验收：** 任意失败题可以通过本地 trace 重放；结果同时包含质量、证据、成本和可靠性；旧 50 题结果标为历史小样本，不冒充稳定结论。

### 阶段 E：服务化和 Atlas 适配器

**目标：** 将 CompassRAG 变成可被完整 Research Agent 调用的独立能力服务。

**文件：**

- Create: `compassrag/api/schemas.py`
- Create: `compassrag/api/routes.py`
- Create: `compassrag/api/server.py`
- Create: `tests/test_research_api.py`
- Modify: `pyproject.toml`

**任务：**

- [ ] 提供 `POST /research`、`GET /runs/{trace_id}` 和 `GET /health`。
- [ ] 请求带 session id、subtask id、预算和幂等键。
- [ ] 返回 answer、evidence、confidence、rounds、stop_reason 和 trace_id。
- [ ] API 只做 schema 校验和 graph runner 调度，不直接操作索引内部结构。
- [ ] 增加超时、错误类型、健康检查和索引版本检查。
- [ ] 为 Atlas 提供本地 adapter 和 HTTP adapter，两者都实现同一 ResearchTool 协议。

**验收：** Atlas 可以用同一接口调用本地或 HTTP CompassRAG；重复请求返回同一 trace；索引缺失、预算耗尽和上游错误都有稳定响应。

## 面试必讲案例

1. **为什么不是一次检索：** 多跳问题的第二跳往往依赖第一跳实体，固定单轮容易证据不完整。
2. **为什么三路检索：** BM25 负责关键词精确匹配，dense 负责语义相似，实体图负责跨文档关联；RRF 只融合排名，避免不同分数空间直接相加。
3. **为什么需要 verifier：** 检索到相关文本不代表答案被支持，claim-level 引用检查可以区分召回成功和回答可信。
4. **为什么要预算上限：** Agent 继续检索的边际收益会下降，必须同时约束轮数、token、延迟和费用。
5. **为什么不让 Atlas 直接读取索引：** CompassRAG 用统一 ResearchTool 协议隔离索引实现，Atlas 只依赖结构化答案和证据，不依赖检索器细节。

## 完成标准

- CompassRAG 可独立回答单个多跳问题，也可作为 Atlas 的 ResearchTool。
- 每个答案有结构化证据、查询来源、停止原因和 trace。
- 工具失败、模型格式错误、证据不足和预算耗尽都能安全收敛。
- 评测可 replay，报告同时包含答案质量、证据支持、成本、延迟和可靠性。
- LangGraph 只承担控制流，不成为证据和业务指标的隐藏状态源。
