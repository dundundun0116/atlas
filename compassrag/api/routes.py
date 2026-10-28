"""HTTP 路由（Task 1/2）：契约校验 → 调度 Agent → 返回结果或事件流。

三种用法：
* **同步**：`POST /research`（`async_mode=false`），等答案返回 `ResearchResult`；
* **异步**：`POST /research`（`async_mode=true`）立刻返回 `RunSummary(queued)`，
  用 `GET /runs/{run_id}/events`（SSE）看进度、`GET /runs/{run_id}` 取结果、`POST /runs/{run_id}/cancel` 取消；
* **回读**：`GET /runs` 列最近运行、`GET /runs/{id}` 取 trace（不重新调模型）。

状态机：`queued → running → completed | failed | cancelled`。事件是 `AgentState.step` 的
实时投影，轨迹仍是唯一真源；客户端断开 SSE 不会中断 Agent。
"""

from __future__ import annotations

import hashlib
import json
import queue
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..agent.core import Agent
from ..agent.research import research
from ..agent.schemas import ResearchRequest, ResearchResult
from .events import EventPublisher, RunRegistry
from .schemas import HealthResponse, ResearchSession, ResearchSubmit, RunEnvelope, RunSummary
from .store import TraceStore

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="compassrag-api")


def index_info(agent: Agent) -> dict[str, dict]:
    """索引规模 + 版本指纹（块 id 前缀哈希），供上游与自己的 manifest 对齐。"""
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


def _sse(event: dict) -> str:
    kind = event.get("kind", "message")
    return f"event: {kind}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def build_router(agent: Agent, *, store: TraceStore,
                 cache: dict[str, ResearchResult] | None = None,
                 registry: RunRegistry | None = None,
                 publisher: EventPublisher | None = None) -> APIRouter:
    router = APIRouter()
    cache = cache if cache is not None else {}
    registry = registry if registry is not None else RunRegistry()
    publisher = publisher if publisher is not None else EventPublisher()

    # -- 运行执行（后台线程） ------------------------------------------- #

    def _execute(request: ResearchRequest, run_id: str) -> None:
        registry.update(run_id, status="running", started_at=time.time())
        publisher.publish(run_id, {"kind": "status", "ts": time.time(),
                                   "payload": {"status": "running"}})

        def on_event(event: dict) -> None:
            publisher.publish(run_id, {"kind": event.get("kind", "step"),
                                       "ts": event.get("ts", time.time()),
                                       "payload": event})
            rec = registry.get(run_id)
            registry.update(run_id, n_events=(rec.n_events + 1) if rec else 1)

        try:
            result = research(agent, request, cache=cache, on_trace=store.put,
                              run_id=run_id, on_event=on_event,
                              cancel_check=lambda: registry.is_cancelled(run_id))
            trace = store.get(result.trace_id) or {}
            usage = trace.get("usage") or {}
            tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
            status = "cancelled" if result.stop_reason == "cancelled" else "completed"
            registry.update(run_id, status=status, trace_id=result.trace_id,
                            answer=result.answer, stop_reason=result.stop_reason,
                            failure=result.failure, confidence=result.confidence,
                            rounds=result.retrieval_rounds, tokens=tokens,
                            finished_at=time.time())
            publisher.publish(run_id, {"kind": "finish", "ts": time.time(),
                                       "payload": {"status": status,
                                                   "stop_reason": result.stop_reason,
                                                   "failure": result.failure}})
        except Exception as exc:  # noqa: BLE001 - 运行失败也要给结构化终态
            registry.update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                            finished_at=time.time())
            publisher.publish(run_id, {"kind": "error", "ts": time.time(),
                                       "payload": {"error": str(exc),
                                                   "type": type(exc).__name__}})
        finally:
            publisher.close(run_id)

    # -- 健康检查 -------------------------------------------------------- #

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            model=getattr(agent.llm, "model", "") or "",
            embedding_model=getattr(agent.llm, "embedding_model", "") or "",
            tools=list(agent.tools), index=index_info(agent),
            n_traces=len(store), n_runs=len(registry.recent(limit=1000)),
        )

    # -- 提交研究 -------------------------------------------------------- #

    @router.post("/research", response_model=None)
    def research_endpoint(payload: ResearchSubmit):
        request = ResearchRequest(**payload.model_dump(exclude={"async_mode"}))
        if payload.async_mode:
            run_id = uuid.uuid4().hex[:12]
            registry.create(run_id, request.query, session_id=request.session_id,
                            subtask_id=request.subtask_id)
            # 先快照再提交：保证响应里的状态就是 queued（不受后台线程抢先完成影响）
            summary = RunSummary(**registry.get(run_id).as_summary())
            _EXECUTOR.submit(_execute, request, run_id)
            return summary

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
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail={"error": "invalid_request", "detail": str(exc)})
        except Exception:  # noqa: BLE001 - 上游异常也要给结构化响应
            return ResearchResult(answer="", failure="upstream_error",
                                  stop_reason="error", trace_id="")

    # -- 运行查询 -------------------------------------------------------- #

    @router.get("/runs", response_model=list[RunSummary])
    def list_runs(limit: int = 50) -> list[RunSummary]:
        return [RunSummary(**row) for row in registry.recent(limit=limit)]

    @router.get("/runs/{run_id}", response_model=RunEnvelope)
    def get_run(run_id: str) -> RunEnvelope:
        rec = registry.get(run_id)
        summary = RunSummary(**rec.as_summary()) if rec is not None else None
        trace_id = rec.trace_id if rec is not None else run_id
        trace = store.get(trace_id) if trace_id else None
        if summary is None and trace is None:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "detail": run_id})
        return RunEnvelope(found=True, summary=summary, trace=trace)

    @router.get("/sessions/{session_id}", response_model=ResearchSession)
    def get_session(session_id: str, include_traces: bool = False) -> ResearchSession:
        runs = [RunSummary(**row) for row in registry.recent(limit=200, session_id=session_id)]
        if not runs:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "detail": session_id})
        return ResearchSession(session_id=session_id, runs=runs)

    # -- 事件流（SSE） --------------------------------------------------- #

    @router.get("/runs/{run_id}/events")
    def stream_events(run_id: str) -> StreamingResponse:
        if registry.get(run_id) is None:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "detail": run_id})

        def gen():
            for event in publisher.history(run_id):
                yield _sse(event)
            if not publisher.is_closed(run_id):
                q = publisher.subscribe(run_id)
                try:
                    while True:
                        try:
                            item = q.get(timeout=1.0)
                        except queue.Empty:
                            if publisher.is_closed(run_id):
                                break
                            yield ": keepalive\n\n"
                            continue
                        if item is publisher.end_marker():
                            break
                        yield _sse(item)
                finally:
                    publisher.unsubscribe(run_id, q)
            yield _sse({"kind": "end", "run_id": run_id, "seq": -1,
                        "ts": time.time(), "payload": {}})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- 取消 ------------------------------------------------------------ #

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict:
        rec = registry.get(run_id)
        if rec is None:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "detail": run_id})
        if rec.is_terminal:
            return {"run_id": run_id, "cancelled": False, "status": rec.status}
        registry.request_cancel(run_id)
        publisher.publish(run_id, {"kind": "status", "ts": time.time(),
                                   "payload": {"status": "cancelling"}})
        return {"run_id": run_id, "cancelled": True, "status": "cancelling"}

    return router
