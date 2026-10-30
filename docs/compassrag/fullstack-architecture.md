# CompassRAG 全栈架构（Frontend ↔ API ↔ Agent ↔ Index）

## 组件与端口

```
浏览器 ──HTTP/SSE──> Vite dev server (:5173) ──proxy──> FastAPI (:8000) ──> Agent(LangGraph)
                         或 nginx (docker)                      │                 │
                                                               │                 ├─ BM25 / dense / KG 工具
                                                               │                 └─ LLM（OpenAI 兼容端点）
                                                               ├─ TraceStore（内存 + runs/api_traces/）
                                                               ├─ RunRegistry + EventPublisher（SSE）
                                                               └─ IngestStore（runs/ingest：文档 + 索引版本）
```

| 服务 | 默认端口 | 说明 |
|---|---|---|
| `scripts/serve_api.py` | 8000 | `/health`、`/research`（sync/async）、`/runs`、`/runs/{id}/events`（SSE）、`/sessions/{id}`、`/documents`、`/index/*` |
| `web/`（Vite dev / nginx） | 5173 / 80 | 工作台：提问、答案、证据、过程时间线、历史运行、语料面板 |
| `scripts/demo.sh` | 8000 + 5173 | 本地一条命令起前后端（不依赖 docker） |
| `docker compose up --build` | 8000 + 5173 | 容器化同构启动（需 Docker Desktop 运行） |

## 两条请求路径

**同步**：`POST /research/sync` → 校验 → `research()` → `ResearchResult`（answer + evidence + confidence + failure）。
**异步**：`POST /research/async` → 立刻返回 `RunSummary(queued)` → 后台线程执行 →
`GET /runs/{id}/events` 以 SSE 推送 `plan / retrieve / verify / answer / finish` 等事件 →
`GET /runs/{id}` 取 `summary + result + trace`；`POST /runs/{id}/cancel` 协作式取消。

事件是 `AgentState.step` 的**实时投影**：轨迹（`AnswerTrace`）仍是唯一真源，SSE 断开不影响执行。

## 状态机与失败语义

`queued → running → completed | failed | cancelled`

| 情形 | 表现 |
|---|---|
| 证据不足 / 空证据 | `completed` + `failure=no_evidence`，答案 `unknown`，`confidence=0` |
| 预算触顶 | `completed` + `failure=budget_exhausted`，`stop_reason=budget_exhausted:*` |
| 客户端取消 | `cancelled`，取消后不再发起模型/检索调用 |
| 超过 `timeout_s` | `failed` + `failure=timeout` |
| 工具/上游异常 | 结构化 `upstream_error`，不 500 崩掉调用方 |
| 请求不合法 | HTTP 422 + `{error: invalid_request}` |

## 索引版本与固定

- `IngestStore` 每次构建生成新版本 `v{时间戳}`（同秒递增后缀），产物含 `chunks.jsonl` + `bm25/`；
  `dense` / `entity_graph` 阶段标 `skipped` 并写原因（需要本地模型 / 付费抽取）。
- 服务启动时**装定一个版本**（`--index-source dataset|ingest` + `--runtime-index-version`），
  每次运行把该版本写进 `RunRecord.index_version`；重建只产生新版本，**不替换正在跑的实例**。
- `GET /index/status` 同时给 `runtime_version` 与 `built_version`，不一致时 `mismatch=true`——
  防止"看到的数字和跑出来的不是同一版索引"。

## 可复现性

- 评测侧：`eval/manifest.py` 记录配置 / 模型 / 语料指纹 / 抽样 / git，`fingerprint()` 不同的 run 不可比。
- 服务侧：`/health` 暴露每个索引的 `size` 与 `version`；`serve_api.py --check` 会与最近一次 manifest 对账。
- 容器：`docker-compose.yml` 里模型与密钥**只从环境变量注入**，镜像不含密钥。

## 已知边界

- 镜像默认只装轻量依赖（BM25 + API）；稠密检索需要 `.[dense]`，实体图需要预构建索引。
- 上传文档构建的索引只有 BM25（没有向量与实体图），所以 `--index-source ingest` 是单路检索。
- 浏览器端到端验证目前是组件测试 + 手工验证；Playwright 用例尚未固化。
