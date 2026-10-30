"""任务 API 路由（Task 1）：薄壳——只做协议转换，业务全在 `atlas/runtime.py`。

好处：调用方与测试可以完全绕开 HTTP（`AgentRuntime` 直接可用），而 HTTP 层只负责
把 `RuntimeFailure` 的 `kind/status_code` 映射成统一错误体。
"""

import time
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from atlas.api import schemas
from atlas.runtime import (
    AgentRuntime,
    ModelTimeout,
    RuntimeFailure,
    StateConflict,
    Unrecoverable,
)
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository
from atlas.tools import ResearchRequest, ResearchToolError


def create_app(repository: GraphRepository,
               tracer_factory: Optional[Callable[[str], Any]] = None,
               research_tool: Any = None) -> FastAPI:
    app = FastAPI(title="Atlas Task Runtime API", version="0.2.0")
    runtime = AgentRuntime(repository, tracer_factory=tracer_factory)

    @app.exception_handler(RuntimeFailure)
    async def _runtime_failure(_request, exc: RuntimeFailure):
        body = schemas.ErrorResponse(
            error=schemas.ErrorBody(
                type=exc.kind, message=exc.message, request_id=exc.request_id
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.get("/healthz", response_model=schemas.HealthView)
    def healthz():
        try:
            sessions = repository.session_count()
            storage = "ok"
        except Exception:  # pragma: no cover - 只在存储真的坏时命中
            sessions, storage = -1, "failed"
        return schemas.HealthView(ok=storage == "ok", storage=storage, sessions=sessions)

    # ---------- 任务生命周期 ----------

    @app.post("/tasks", response_model=schemas.AdvanceView)
    def create_task(payload: schemas.CreateTaskRequest):
        return schemas.AdvanceView(**runtime.create_task(
            payload.session_id, payload.goal,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/advance", response_model=schemas.AdvanceView)
    def advance_task(session_id: str, payload: schemas.AdvanceRequest):
        return schemas.AdvanceView(**runtime.advance_task(
            session_id, payload.trigger, reason=payload.reason,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/pause", response_model=schemas.AdvanceView)
    def pause_task(session_id: str, payload: schemas.PauseRequest):
        return schemas.AdvanceView(**runtime.pause_task(
            session_id, reason=payload.reason,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/resume", response_model=schemas.AdvanceView)
    def resume_task(session_id: str, payload: schemas.RequestMeta):
        return schemas.AdvanceView(**runtime.resume_task(
            session_id, request_id=payload.request_id,
            idempotency_key=payload.idempotency_key,
        ))

    # ---------- 研究（可选：部署时注入 ResearchTool） ----------

    @app.post("/tasks/{session_id}/research", response_model=schemas.ResearchCallView)
    def research_task(session_id: str, payload: schemas.ResearchCallRequest):
        if research_tool is None:
            raise Unrecoverable(
                "本部署未配置 ResearchTool（启动参数 --compassrag-url）", request_id=payload.request_id
            )
        runtime.get_state(session_id)  # 任务必须存在（不存在 → 404）
        request = ResearchRequest(
            session_id=session_id,
            subtask_id=payload.subtask_id,
            query=payload.query,
            budget_tokens=payload.budget_tokens,
            max_rounds=payload.max_rounds,
        )
        started = time.perf_counter()
        try:
            result = research_tool.research(request)
        except ResearchToolError as exc:
            repository.record_trace(session_id, {
                "trace_id": exc.trace_id or request.trace_id,
                "agent_role": "research", "tool_name": "compassrag",
                "outcome": exc.kind, "stop_reason": exc.kind,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            if exc.kind == "timeout":
                # 上游超时走统一错误体（504 model_timeout），而不是 200 的成功信封
                raise ModelTimeout(exc.message or "研究工具超时", request_id=payload.request_id)
            return schemas.ResearchCallView(
                session_id=session_id, failure_kind=exc.kind,
                failure_message=exc.message, trace_id=exc.trace_id or request.trace_id,
            )
        service = ServiceRuntime(repository, session_id)
        row = service.note(
            "research", result.answer, kind="fact",
            reason=(f"检索 {result.retrieval_rounds} 轮，confidence={result.confidence:.2f}，"
                    f"stop={result.stop_reason or 'unknown'}"),
            evidence=result.evidence_json(),
            source=",".join(item.source for item in result.evidence[:3]),
            confidence=result.confidence,
            idempotency_key=request.idempotency_key,
            meta={
                "research_trace_id": result.trace_id,
                "research_stop_reason": result.stop_reason,
                "research_rounds": result.retrieval_rounds,
                "research_confidence": result.confidence,
            },
        )
        repository.record_trace(session_id, {
            "trace_id": result.trace_id or request.trace_id,
            "agent_role": "research", "tool_name": "compassrag",
            "outcome": "ok", "stop_reason": result.stop_reason,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        return schemas.ResearchCallView(
            session_id=session_id, fact_id=row.node_id, answer=result.answer,
            evidence=[item.to_dict() for item in result.evidence],
            confidence=result.confidence, stop_reason=result.stop_reason,
            trace_id=result.trace_id or request.trace_id,
        )

    # ---------- 读 ----------

    @app.get("/tasks/{session_id}", response_model=schemas.TaskStateView)
    def get_task(session_id: str):
        return schemas.TaskStateView(**runtime.get_state(session_id))

    @app.post("/tasks/{session_id}/restore", response_model=schemas.TaskStateView)
    def restore_task(session_id: str):
        return schemas.TaskStateView(**runtime.restore_task(session_id))

    @app.get("/tasks/{session_id}/trace", response_model=schemas.TraceView)
    def get_trace(session_id: str):
        return schemas.TraceView(**runtime.get_trace(session_id))

    @app.get("/tasks/{session_id}/report", response_model=schemas.ReportView)
    def get_report(session_id: str):
        return schemas.ReportView(**runtime.get_report(session_id))

    @app.get("/tasks/{session_id}/replay")
    def replay_task(session_id: str) -> Dict[str, Any]:
        return runtime.replay(session_id)

    return app
