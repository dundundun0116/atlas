# Atlas + CompassRAG 系统总流程与设计（草案）

> 状态：方案草案。本文用于统一两个现有项目的职责和集成验收，不代表新的架构决策已经定稿。Atlas 与 CompassRAG 继续各自维护；简历上可作为一个完整的长程研究 Agent 项目展示。

## 1. 项目目标

构建一个能够完成多步骤研究任务的 Agent：Atlas 负责规划任务、保存跨步骤状态、协调角色、处理暂停与恢复；CompassRAG 负责围绕单个研究问题检索材料、组织证据并核验回答。

用户看到的是一个可以提交研究任务、观察证据和执行过程、查看最终报告的系统。系统必须能回答“结论依据什么材料”“中间任务做到哪一步”“失败后能否恢复”，而不只是返回一段生成文本。

## 2. 总体分工

| 部分 | 单一职责 | 不负责 |
|---|---|---|
| Atlas | 长程任务、子任务依赖、共享状态图、角色协作、上下文结转、暂停/恢复、总任务 trace | 文档切分、BM25/向量/实体图检索、单次研究问题的检索决策 |
| CompassRAG | 研究请求内的问题路由、多跳检索、查询改写、实体图检索、证据充分度判断、claim 核验、有限修复与拒答 | 用户总目标拆解、跨子任务长期状态、Atlas 的任务生命周期 |
| ResearchTool 协议 | 传递研究请求、证据、停止原因、错误和关联 ID；隔离本地调用与 HTTP 服务差异 | 承载任一方的业务状态或取代其内部模型 |
| UI / API | 提交任务、查状态、查看证据与过程、取消或恢复任务 | 自行实现 Agent 决策逻辑 |

核心边界：Atlas 的子任务调用 CompassRAG；CompassRAG 不反向调用 Atlas，不读写 Atlas 数据库。Atlas 保存被选用的结论和来源引用，CompassRAG 保留自己的检索轨迹和索引状态。

## 3. 总体架构

```text
用户 / Web UI / CLI
        |
        v
Atlas FastAPI
        |
        v
AgentRuntime -----------------------> Atlas Repository
        |                               tasks / graph / events /
        |                               checkpoints / traces
        v
Atlas Orchestrator (LangGraph 控制流)
  Planner -> Research -> Worker -> Auditor -> Repair / Finish
     |          |          |          |
     |          |          |          +-- 检查约束、事实来源和产出
     |          |          +------------- 形成阶段产出
     |          +------------------------ ResearchTool
     +----------------------------------- 规划依赖和预算
                    |
                    v
             CompassRAG Adapter
             Local 或 HTTP
                    |
                    v
             CompassRAG Research API
                    |
                    v
       Agent -> Planner / Retriever tools / Verifier
                    |
                    v
       BM25 + Dense + Entity Graph + Source Corpus
```

LangGraph 只表达 Atlas 的控制流。任务状态、业务事件、checkpoint 和图记忆的唯一真源是 Atlas Repository；不得让 LangGraph checkpoint 与 Repository 同时成为业务状态真源。

## 4. 端到端任务流程

### 4.1 创建任务与约束登记

1. 用户提交目标、输出格式、范围或资料库选择。
2. Atlas 校验请求并创建 `Goal`、`Constraint` 和初始 `Subtask` 节点。
3. `AgentRuntime` 生成或接收 `request_id`、`session_id` 和幂等键，持久化初始状态及事件。
4. 任务进入 `PLANNING`。重复提交相同幂等请求返回已有结果，不创建第二个任务。

### 4.2 规划与依赖建立

1. Planner 读取 Atlas 的当前状态视图和用户约束。
2. Planner 产出有依赖关系的子任务、研究问题、验收条件和任务级预算。
3. Atlas 校验计划格式、依赖引用和有向无环约束后写入 `Subtask` / `Decision` 节点。
4. 计划无效时有限重试；仍无效则记录失败原因并进入可解释的终态，不让未校验的模型输出直接改图。

### 4.3 研究取证

1. Atlas 按依赖顺序选取一个可执行子任务，从用户目标、子任务和已确认状态生成研究查询。
2. Atlas 通过 `ResearchTool` 发送查询、该子任务的轮数/token 上限、trace ID 和幂等键。
3. CompassRAG 在单次研究请求内决定直答或多跳检索，必要时调用查询改写、BM25、向量和实体图检索工具；根据证据充分度决定继续检索、核验、修复或拒答。
4. CompassRAG 返回阶段性结论、可定位证据、置信度、停止原因和 trace ID。
5. Atlas 校验响应契约和预算，将结论写成带来源的 `Fact`；必要时把较长研究结果保存为 `Artifact`，并建立子任务到事实/产物的关系。
6. 只有结构合法、来源可追溯且没有被拒答的结果才能作为后续 Worker 的有效证据。无证据、工具失败或拒答不得伪装成事实。

### 4.4 执行、审核和修订

1. Worker 从状态图读取当前约束、依赖子任务结论及其来源，形成阶段产出 `Artifact`。
2. Auditor 检查产出是否回答目标、是否违反约束、关键主张是否能追溯到有效证据。
3. 审核通过则标记子任务完成；审核不通过则记录具体问题，进入一次有上限的 Repair。
4. 修订产物通过 `supersedes` 关系替代旧产物，旧内容保留用于审计但不再作为当前有效答案。
5. 达到修订或预算上限仍不通过时，任务失败或返回部分结果，并明确未完成原因。

### 4.5 长任务上下文与状态

子任务结束时或上下文预算需要收敛时，Atlas 将历史转成结构化状态并组装下一轮上下文。上下文包括当前计划与约束、依赖子任务的有效结论、必要的近期交互和风险提示。过期或已被 supersede 的信息不能作为有效事实送给角色；冲突来源需要明确标示，交由审核或后续研究处理。

CompassRAG 的检索 trace 是“单次研究调用的证据”，Atlas 的 graph/event/trace 是“跨子任务执行状态”。Atlas 保留 CompassRAG 的 trace ID 和引用，不复制其全部内部轨迹作为另一份业务状态。

### 4.6 完成、暂停、恢复与回放

- 所有子任务验收完成后，Atlas 汇总有效产物和来源，生成最终报告并进入 `COMPLETED`。
- 用户主动暂停、需要补充输入或遇到可恢复中断时，保存 checkpoint 并进入 `PAUSED`。
- 恢复时读取 checkpoint 与事件版本，跳过已提交的状态写入；未完成的工具调用按幂等键策略续跑。
- `FAILED` 任务保留失败步骤、错误类别、预算用量和已完成子任务；是否允许显式重试由任务 API 决定。
- Replay 读取 Atlas 的事件和 trace，还原任务时间线；查看既有 CompassRAG trace 不重新调用模型。

## 5. ResearchTool 契约

Atlas 侧已经有 `ResearchRequest`、`ResearchResult`、`EvidenceItem`、`ResearchTool` 与本地/HTTP 适配器。集成时以现有协议为基准，先通过契约测试核对 CompassRAG 当前请求/响应 schema，不为追求字段统一而盲目复制内部 schema。

请求语义至少包含：

| 字段 | 含义 |
|---|---|
| `session_id` | Atlas 长任务 ID，用于关联与追踪 |
| `subtask_id` | 当前研究子任务 |
| `query` | 单次需要查证的问题 |
| `budget_tokens` / `max_rounds` | 此次研究的资源上限 |
| `idempotency_key` | 同一次逻辑调用重试时保持不变 |
| `trace_id` | Atlas 与 CompassRAG trace 的关联标识 |

响应语义至少包含：

| 字段 | 含义 |
|---|---|
| `answer` | 阶段性研究结论；拒答时为空或按明确约定返回 |
| `evidence[]` | 来源、chunk 定位、引文和检索器/排序信息（对 Atlas 暴露必要字段） |
| `confidence` | CompassRAG 对研究结果的置信度信号，不等同于审核通过 |
| `retrieval_rounds` / `stop_reason` | 研究过程摘要和停止原因 |
| `trace_id` | CompassRAG 研究 trace，可与 Atlas 任务关联 |
| `failure` | 可分类错误；不得混成普通自然语言答案 |

接口演进要求：字段新增应向后兼容；未知可选字段忽略；必需字段缺失归类为契约错误；错误类别固定且由 Atlas 决定重试、降级、等待或终止。

## 6. 状态与数据所有权

| 数据 | 权威存储 | 生命周期 |
|---|---|---|
| 用户目标、约束、子任务和依赖 | Atlas Repository | 整个任务 |
| 当前有效决策、事实和产物关系 | Atlas State Graph / Repository | 跨子任务；可被 supersede 或过期 |
| Atlas 事件、checkpoint、API 幂等结果和角色 trace | Atlas Repository | 支持审计、恢复和 replay |
| 文档、chunk、BM25/向量/实体图索引 | CompassRAG 数据与索引 | 语料或索引版本 |
| 单次检索轨迹、工具调用和 claims | CompassRAG trace store | 单次研究调用；Atlas 通过 ID 引用 |

Atlas 保存必要的引用和结论，不把整个文档库灌入 Atlas 图；CompassRAG 不承担跨任务长期记忆。

## 7. 失败处理原则

| 失败 | CompassRAG / Adapter 行为 | Atlas 行为 |
|---|---|---|
| 请求参数不合法 | 返回稳定的 invalid request 错误 | 修正前不重试，记录失败并定位请求 |
| 没有相关证据 | 返回 no evidence / 明确拒答 | 不写 `Fact`；可标子任务待补证或部分完成 |
| 检索预算用尽 | 返回 budget exhausted 和停止原因 | 不超过任务总预算；是否缩小问题由 Planner 决定 |
| 超时或上游不可用 | 标记 timeout/upstream error；安全情况下有限重试 | 复用幂等键；超出策略后暂停或失败，不写伪事实 |
| 响应 JSON/schema 错误 | Adapter 拒绝无效响应 | 记录为契约错误并停止本次写入 |
| 审核发现证据冲突 | 返回来源与冲突线索 | 保留来源，标记冲突并计划补查/升级审核 |
| 进程中断 | CompassRAG 已完成请求由幂等保护 | Atlas 从 checkpoint 恢复，防止重复提交已完成状态变更 |

重试必须有上限。对于已经开始输出的流式研究请求，不应假设客户端断连等于服务端任务已停止；取消和结果落库语义必须有单独的 API 契约测试。

## 8. 运行形态

### 开发与演示

优先以一个进程内组合入口运行 Atlas 和 CompassRAG 本地 adapter，减少启动步骤，适合可重复演示和端到端测试。两侧仍通过 `ResearchTool` 协议隔离。

### 服务化

Atlas API 和 CompassRAG Research API 分别启动。Atlas 使用 HTTP adapter 调用 CompassRAG，配置服务地址、超时和任务预算；两个服务各自管理数据库/索引和 trace。部署说明需要写清语料索引版本与 CompassRAG 服务版本不一致时的提示。

不要求为了展示“微服务”而强拆更多服务；当前阶段不引入新的消息队列或数据库作为简历装饰。

## 9. 集成实施路线与验收

### 阶段 0：核对现状与冻结边界

- [ ] 以当前 Atlas / CompassRAG 工作树为输入，记录双方 schema、错误映射、HTTP 路由和运行方式。
- [ ] CompassRAG 有未提交改动时，先在其现有工作基础上集成，不清理或覆盖无关改动。
- [ ] 确认 Atlas 的内存与 HTTP adapter 对同一 ResearchResult 语义一致。

**验收：** 一页接口对照表；无代码路径绕过 ResearchTool 直接读 CompassRAG 内部状态。

### 阶段 1：契约与本地端到端

- [ ] 用确定性 fake 工具锁定完整任务流程：创建、规划、研究取证、写入图、生成产物、审核、修订或完成。
- [ ] 用 CompassRAG 本地实现替换 fake，核验真实 evidence 的来源、chunk 定位和 trace ID 能进入 Atlas。
- [ ] 覆盖空证据、拒答、schema 错误、重复幂等键和工具异常。

**验收：** 一条命令运行完整示例；工具失败不会写入有效事实；重复调用不产生重复有效节点。

### 阶段 2：HTTP 服务化与运行恢复

- [ ] 通过 HTTP adapter 运行同一任务，不改变 Atlas 编排逻辑。
- [ ] 测试服务超时、取消、上游 4xx/5xx、重复请求、CompassRAG trace 查询和 Atlas 重启恢复。
- [ ] 检查 CompassRAG 同步/异步研究 API 的完成、取消和事件订阅语义，确定 Atlas 采用何种调用方式。

**验收：** 本地 adapter 与 HTTP adapter 通过同一组 ResearchTool 契约测试；重启后不会重复写入已提交事实。

### 阶段 3：统一演示与文档

- [ ] 一个用户入口可创建任务、查看状态、证据、角色步骤、失败原因和最终报告。
- [ ] 展示 Atlas task trace 与 CompassRAG research trace 的关联，而不是合并成难以区分的一条日志。
- [ ] README 提供最短启动路径、架构图、示例问题、局限和评测口径。

**验收：** 新环境照文档可启动；演示中可以看见一次有证据引用的研究任务和一条失败/恢复路径。

### 阶段 4：系统级评测

- [ ] Atlas 评测任务规划、状态保留、约束遵守、恢复和回放。
- [ ] CompassRAG 评测多跳 QA、证据覆盖、claim 支持和检索成本。
- [ ] 端到端另建任务级评测集，覆盖多子问题、来源冲突、需要修订和工具失败。
- [ ] 每个结果保存模型、索引、预算、题目集版本和重复次数；小样本仅作演示，不写成普适效果结论。

**验收：** 能从总任务报告下钻到 Atlas 子任务和 CompassRAG 证据 trace；两个项目原有指标仍保持各自口径。

## 10. 简历表达草案

项目名：**Atlas Research Agent（Atlas + CompassRAG）**

> 面向多步骤研究任务，整合 Atlas 的共享状态记忆、任务恢复与多角色协作，以及 CompassRAG 的多跳检索和证据核验；通过统一研究工具协议衔接本地与 HTTP 调用，并保留跨子任务结论来源和端到端执行轨迹。

最终简历要等阶段 1–3 的集成验收通过后再定稿。效果数字只从可复现的系统级评测中选取，并注明任务集、样本量和运行设置。

## 11. 暂不纳入本方案的内容

- 将两个仓库合并成一个代码仓库。
- 把 CompassRAG 的检索实现复制进 Atlas。
- 引入新框架、数据库或消息队列，仅为扩充技术栈。
- 宣称通用长程 Agent 效果，或把离线脚手架和小样本结果写成简历提升数据。
- 将 Atlas 定位改成通用 Agent 平台，或将 CompassRAG 改成 Atlas 的内部子包。
