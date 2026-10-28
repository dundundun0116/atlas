"""HTTP 路由（阶段 E）。

三条端点都只做三件事：**校验 schema → 调度 graph/agent → 包装结果**。
不直接碰索引内部结构，也允许注入任意 `Agent`（测试里用假 LLM，生产里用真检索器）。

失败语义与 `ResearchResult.failure` 同一套词表，Atlas 可以据此分支：
`no_evidence` / `budget_exhausted` / `upstream_error` / `timeout` / `invalid_request`。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi import APIRouter, HTTPException

import hashlib

from ..agent.core import Agent
from ..agent.research import research
from ..agent.schemas import ResearchRequest, ResearchResult
from .schemas import HealthResponse, RunEnvelope
from .store import TraceStore

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="compassrag-api")


def index_info(agent: Agent) -> dict[str, dict]:
    """索引规模 + 版本指纹。

    版本指纹只取块 id 的前缀哈希：足够发现"索引换了/语料重建了"，又不至于在 health
    里把几十万 id 序列化出去。上游把它与自己的 manifest 对齐即可做版本检查。
    """
    out: dict[str, dict] = {}
    for name, tool in agent.tools.items():
        info: dict = {"ok": True}
        chunk_ids = getattr(tool, "chunk_ids", None)
        if chunk_ids is not None:
            info["size"] = len(chunk_ids)
            digest = hashlib.sha1(",".join(map(str, chunk_ids[:8])).encode()).hexdigest()[:12]
            info["version"] = f"{len(chunk_ids)}:{digest}"
        graph = getattr(tool, "graph", None)
        if graph is not None:
            nodes = getattr(graph, "nodes", None)
            info["nodes"] = len(nodes) if hasattr(nodes, "__len__") else 0
        out[name] = info
    return out


def build_router(agent: Agent, *, store: TraceStore,
                 cache: dict[str, ResearchResult] | None = None) -> APIRouter:
    router = APIRouter()
    cache = cache if cache is not None else {}

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            model=getattr(agent.llm, "model", "") or "",
            embedding_model=getattr(agent.llm, "embedding_model", "") or "",
            tools=list(agent.tools),
            index=index_info(agent),
            n_traces=len(store),
        )

    @router.post("/research", response_model=ResearchResult)
    def research_endpoint(request: ResearchRequest) -> ResearchResult:
        try:
            if request.timeout_s and request.timeout_s > 0:
                future = _EXECUTOR.submit(research, agent, request, cache=cache,
                                          on_trace=store.put)
                try:
                    return future.result(timeout=request.timeout_s)
                except FutureTimeout:
                    future.cancel()
                    return ResearchResult(answer="", stop_reason="", trace_id="",
                                          failure="timeout")
            return research(agent, request, cache=cache, on_trace=store.put)
        except ValueError as exc:      # 请求本身不合法（空 query 等已由 schema 拦住）
            raise HTTPException(status_code=422,
                                detail={"error": "invalid_request", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 上游异常也要给结构化响应，不能 500 崩掉调用方
            return ResearchResult(answer="", failure="upstream_error",
                                  stop_reason="error", trace_id="")

    @router.get("/runs/{trace_id}", response_model=RunEnvelope)
    def get_run(trace_id: str) -> RunEnvelope:
        trace = store.get(trace_id)
        if trace is None:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "detail": trace_id})
        return RunEnvelope(found=True, trace=trace)

    return router
