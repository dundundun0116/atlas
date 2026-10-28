"""任务 API 路由（阶段 E）：创建 / 推进 / 查状态 / 恢复 / 读 trace / 取报告。

设计约束：
- 路由只做参数校验、幂等判定和调用编排，不直接改图（写全部经 `GraphRepository`）；
- 幂等结果落在事件表里，**跨进程有效**，所以服务重启后重复请求仍安全；
- 错误统一成六类，客户端按 `error.type` 决策，不看堆栈。
"""

from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from atlas.api import schemas
from atlas.context.provenance import audit_nodes
from atlas.memory.schema import NodeStatus
from atlas.observability.tracing import Tracer
from atlas.orchestration.state_machine import IllegalTransition, TaskStateMachine
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository

#: 触发词 → 下一步该谁上场（写进 checkpoint 的 pending_role 与 trace 的 agent_role）
NEXT_ROLE = {
    "plan_ready": "worker",
    "work_done": "auditor",
    "audit_repair": "worker",
    "repair_done": "auditor",
}


class ApiError(RuntimeError):
    """带 HTTP 语义的领域错误。"""

    def __init__(self, status_code: int, type_: str, message: str, request_id: str = ""):
        if type_ not in schemas.ERROR_TYPES:
            raise ValueError(f"未知错误类型：{type_}")
        super().__init__(message)
        self.status_code = status_code
        self.type = type_
        self.message = message
        self.request_id = request_id


def create_app(repository: GraphRepository, tracer_factory: Optional[Callable[[str], Tracer]] = None) -> FastAPI:
    app = FastAPI(title="Atlas Task API", version="0.1.0")
    repo = repository

    # ---------- 基础设施 ----------

    def _rid(request_id: str) -> str:
        return request_id or f"req-{uuid4().hex[:12]}"

    def _tracer(session_id: str) -> Tracer:
        if tracer_factory is not None:
            return tracer_factory(session_id)

        def sink(event) -> None:
            repo.record_trace(session_id, event.to_dict())

        return Tracer(run_id=session_id, sink=sink)

    def _runtime(session_id: str, goal: Optional[str] = None) -> ServiceRuntime:
        row = repo.get_session(session_id)
        if row is None:
            raise ApiError(404, "not_found", f"会话不存在：{session_id}")
        runtime = ServiceRuntime(repo, session_id)
        runtime.resume()
        if runtime.machine is None:
            runtime.machine = TaskStateMachine()
        return runtime

    def _idempotent(session_id: str, key: str, operation: str,
                    produce: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        if key:
            cached = repo.get_idempotent_result(session_id, key)
            if cached is not None:
                replay = dict(cached)
                replay["replayed"] = True
                return replay
        result = produce()
        if key:
            repo.record_idempotent_result(session_id, key, operation, result)
        return result

    def _state_view(runtime: ServiceRuntime, session_id: str) -> schemas.TaskStateView:
        row = repo.get_session(session_id)
        engine = runtime.engine
        audit = audit_nodes(engine.nodes.values())
        view = runtime.state_view()
        return schemas.TaskStateView(
            session_id=session_id,
            goal=row.goal if row is not None else "",
            state=view["state"],
            nodes=[
                schemas.NodeView(
                    id=n.id, type=n.type.value, status=n.status.value, content=n.content,
                    actor=n.actor, source=n.source, confidence=n.confidence,
                    valid_until=n.valid_until, verified_at=n.verified_at,
                )
                for n in sorted(engine.nodes.values(), key=lambda n: n.id)
            ],
            edges=[schemas.EdgeView(src=e.src, dst=e.dst, type=e.type.value) for e in engine.edges],
            checkpoint=(
                schemas.CheckpointView(**view["checkpoint"]) if view["checkpoint"] else None
            ),
            provenance=audit.to_dict(),
        )

    def _report(runtime: ServiceRuntime, session_id: str) -> str:
        engine = runtime.engine
        lines = ["# 任务报告", f"状态: {runtime.machine.state.value if runtime.machine else 'unknown'}"]
        goal = engine.current_goal()
        session_row = repo.get_session(session_id)
        goal_text = goal.content if goal is not None else (session_row.goal if session_row else "")
        if goal_text:
            lines.append(f"目标: {goal_text}")
        decisions = engine.active_decisions()
        if decisions:
            lines.append("关键决定:")
            lines += [f"  - [{d.id}] {d.content}（{d.reason}）" for d in decisions]
        artifacts = [
            n for n in sorted(engine.nodes.values(), key=lambda n: n.id)
            if n.type.value == "artifact" and n.status is NodeStatus.ACTIVE
        ]
        if artifacts:
            lines.append("产出物:")
            lines += [f"  - [{a.id}] {a.content}" for a in artifacts]
        if runtime.machine is not None and runtime.machine.failure_reason:
            lines.append(f"失败原因: {runtime.machine.failure_reason}")
        return "\n".join(lines)

    # ---------- 错误 ----------

    @app.exception_handler(ApiError)
    async def _api_error(_request, exc: ApiError):
        body = schemas.ErrorResponse(
            error=schemas.ErrorBody(type=exc.type, message=exc.message, request_id=exc.request_id)
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    # ---------- 端点 ----------

    @app.get("/healthz", response_model=schemas.HealthView)
    def healthz():
        try:
            sessions = repo.session_count()
            storage = "ok"
        except Exception:
            sessions, storage = -1, "failed"
        return schemas.HealthView(ok=storage == "ok", storage=storage, sessions=sessions)

    @app.post("/tasks", response_model=schemas.AdvanceView)
    def create_task(payload: schemas.CreateTaskRequest):
        request_id = _rid(payload.request_id)

        def produce() -> Dict[str, Any]:
            if repo.get_session(payload.session_id) is None:
                repo.create_session(payload.session_id, goal=payload.goal)
            runtime = _runtime(payload.session_id)
            return {
                "session_id": payload.session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        try:
            return _idempotent(payload.session_id, payload.idempotency_key, "create_task", produce)
        except ApiError:
            raise
        except Exception as exc:  # 存储层故障
            raise ApiError(503, "storage_failure", f"{type(exc).__name__}: {exc}", request_id)

    @app.post("/tasks/{session_id}/advance", response_model=schemas.AdvanceView)
    def advance_task(session_id: str, payload: schemas.AdvanceRequest):
        request_id = _rid(payload.request_id)

        def produce() -> Dict[str, Any]:
            runtime = _runtime(session_id)
            tracer = _tracer(session_id)
            try:
                runtime.machine.apply(payload.trigger, payload.reason)
            except IllegalTransition as exc:
                raise ApiError(409, "state_conflict", str(exc), request_id)
            except ValueError as exc:
                raise ApiError(400, "invalid_request", str(exc), request_id)
            pending_role = NEXT_ROLE.get(payload.trigger, "")
            runtime.checkpoint(pending_role=pending_role)
            tracer.record(
                agent_role=pending_role or "system",
                tool_name="state_machine",
                step_id=payload.trigger,
                state_version=len(repo.nodes(session_id)),
                outcome="ok",
                stop_reason=payload.reason,
            )
            return {
                "session_id": session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        try:
            return _idempotent(session_id, payload.idempotency_key, "advance_task", produce)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(503, "storage_failure", f"{type(exc).__name__}: {exc}", request_id)

    @app.get("/tasks/{session_id}", response_model=schemas.TaskStateView)
    def get_task(session_id: str):
        runtime = _runtime(session_id)
        return _state_view(runtime, session_id)

    @app.post("/tasks/{session_id}/restore", response_model=schemas.TaskStateView)
    def restore_task(session_id: str):
        row = repo.latest_checkpoint(session_id)
        if row is None:
            raise ApiError(409, "state_conflict", "没有可恢复的 checkpoint")
        runtime = ServiceRuntime(repo, session_id)
        runtime.resume()
        if runtime.machine is None:
            raise ApiError(409, "state_conflict", "checkpoint 中没有状态机快照")
        return _state_view(runtime, session_id)

    @app.get("/tasks/{session_id}/trace", response_model=schemas.TraceView)
    def get_trace(session_id: str):
        _runtime(session_id)
        traces = repo.traces(session_id)
        events = [schemas.TraceEventView(**payload) for payload in traces]
        summary = {
            "events": len(events),
            "tokens": sum(e.token_usage for e in events),
            "latency_ms": round(sum(e.latency_ms for e in events), 2),
            "errors": sum(1 for e in events if e.outcome != "ok"),
            "retries": sum(e.retries for e in events),
        }
        return schemas.TraceView(session_id=session_id, events=events, summary=summary)

    @app.get("/tasks/{session_id}/report", response_model=schemas.ReportView)
    def get_report(session_id: str):
        runtime = _runtime(session_id)
        return schemas.ReportView(
            session_id=session_id,
            state=runtime.machine.state.value if runtime.machine else "unknown",
            report=_report(runtime, session_id),
        )

    return app
