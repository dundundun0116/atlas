"""公共运行时门面（Task 1）：外部只接触任务、状态、事件和结果契约。

调用方不需要知道图引擎、ORM、事件表或 LangGraph 的存在：这里把
「创建 / 推进 / 暂停 / 恢复 / 查状态 / 读 trace / 回放」收敛成同一种返回值形状，
错误收敛成稳定分类。HTTP 层（`atlas/api/routes.py`）只是这个门面的薄壳。
"""

import threading
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from atlas.context.provenance import audit_nodes
from atlas.memory.schema import NodeStatus, NodeType
from atlas.observability.tracing import Tracer
from atlas.orchestration.state_machine import IllegalTransition, TaskState, TaskStateMachine
from atlas.service import ServiceRuntime
from atlas.storage import events as storage_events
from atlas.storage.repository import GraphRepository

#: 触发词 → 下一步该谁上场
NEXT_ROLE = {
    "plan_ready": "worker",
    "work_done": "auditor",
    "audit_repair": "worker",
    "repair_done": "auditor",
}


class RuntimeFailure(Exception):
    """运行时错误基类：`kind` 与 `status_code` 给 HTTP 层直接用。"""

    kind = "unrecoverable"
    status_code = 500

    def __init__(self, message: str, request_id: str = ""):
        super().__init__(message)
        self.message = message
        self.request_id = request_id


class InvalidRequest(RuntimeFailure):
    kind = "invalid_request"
    status_code = 400


class StateConflict(RuntimeFailure):
    kind = "state_conflict"
    status_code = 409


class NotFound(RuntimeFailure):
    kind = "not_found"
    status_code = 404


class StorageFailure(RuntimeFailure):
    kind = "storage_failure"
    status_code = 503


class ModelTimeout(RuntimeFailure):
    kind = "model_timeout"
    status_code = 504


class Unrecoverable(RuntimeFailure):
    kind = "unrecoverable"
    status_code = 500


class AgentRuntime:
    """任务运行时：会话、幂等、状态机、checkpoint、trace、replay 都在这里。"""

    def __init__(self, repository: GraphRepository,
                 tracer_factory: Optional[Callable[[str], Tracer]] = None):
        self.repository = repository
        self.tracer_factory = tracer_factory
        self._lock_guard = threading.Lock()
        self._session_locks: Dict[str, threading.RLock] = {}

    # ---------- 任务生命周期 ----------

    def create_task(self, session_id: str, goal: str, *, request_id: str = "",
                    idempotency_key: str = "") -> Dict[str, Any]:
        request_id = request_id or self._new_request_id()
        if not str(session_id or "").strip():
            raise InvalidRequest("session_id 不能为空", request_id)
        if not str(goal or "").strip():
            raise InvalidRequest("goal 不能为空", request_id)

        def produce() -> Dict[str, Any]:
            if self.repository.get_session(session_id) is None:
                self.repository.create_session(session_id, goal=goal)
            runtime = self._load(session_id)
            return {
                "session_id": session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        return self._guard(request_id, lambda: self._idempotent(
            session_id, idempotency_key, "create_task", produce))

    def advance_task(self, session_id: str, trigger: str, *, reason: str = "",
                     request_id: str = "", idempotency_key: str = "") -> Dict[str, Any]:
        request_id = request_id or self._new_request_id()

        def produce() -> Dict[str, Any]:
            runtime = self._load(session_id)
            tracer = self._tracer(session_id)
            try:
                runtime.machine.apply(trigger, reason)
            except IllegalTransition as exc:
                raise StateConflict(str(exc), request_id) from exc
            except ValueError as exc:
                raise InvalidRequest(str(exc), request_id) from exc
            pending_role = NEXT_ROLE.get(trigger, "")
            runtime.checkpoint(pending_role=pending_role)
            tracer.record(
                agent_role=pending_role or "system",
                tool_name="state_machine",
                step_id=trigger,
                state_version=len(self.repository.nodes(session_id)),
                outcome="ok",
                stop_reason=reason,
            )
            return {
                "session_id": session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        return self._guard(request_id, lambda: self._idempotent(
            session_id, idempotency_key, "advance_task", produce))

    def pause_task(self, session_id: str, *, reason: str = "", request_id: str = "",
                   idempotency_key: str = "") -> Dict[str, Any]:
        request_id = request_id or self._new_request_id()

        def produce() -> Dict[str, Any]:
            runtime = self._load(session_id)
            try:
                runtime.machine.apply("pause", reason or "等待外部输入")
            except IllegalTransition as exc:
                raise StateConflict(str(exc), request_id) from exc
            runtime.checkpoint(pending_role="")
            return {
                "session_id": session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        return self._guard(request_id, lambda: self._idempotent(
            session_id, idempotency_key, "pause_task", produce))

    def resume_task(self, session_id: str, *, request_id: str = "",
                    idempotency_key: str = "") -> Dict[str, Any]:
        """从 checkpoint 继续：暂停中的任务回到暂停前状态；已恢复过则拒绝。"""
        request_id = request_id or self._new_request_id()

        def produce() -> Dict[str, Any]:
            runtime = self._load(session_id)
            checkpoint = self.repository.latest_checkpoint(session_id)
            if checkpoint is None:
                raise StateConflict("没有可恢复的 checkpoint", request_id)
            if runtime.machine.state is not TaskState.PAUSED:
                # 只能从暂停态继续：重复恢复会走到这里，明确报冲突而不是静默重跑
                raise StateConflict(
                    f"当前状态 {runtime.machine.state.value} 不在暂停态，不能恢复", request_id
                )
            runtime.machine.apply("resume", "从 checkpoint 继续")
            runtime.checkpoint(pending_role=NEXT_ROLE.get("resume", ""))
            return {
                "session_id": session_id,
                "request_id": request_id,
                "state": runtime.machine.state.value,
                "replayed": False,
            }

        return self._guard(request_id, lambda: self._idempotent(
            session_id, idempotency_key, "resume_task", produce))

    # ---------- 读 ----------

    def get_state(self, session_id: str) -> Dict[str, Any]:
        runtime = self._load(session_id)
        row = self.repository.get_session(session_id)
        engine = runtime.engine
        audit = audit_nodes(engine.nodes.values())
        view = runtime.state_view()
        return {
            "session_id": session_id,
            "goal": row.goal if row is not None else "",
            "state": view["state"],
            "nodes": [
                {
                    "id": n.id, "type": n.type.value, "status": n.status.value,
                    "content": n.content, "actor": n.actor, "source": n.source,
                    "confidence": n.confidence, "valid_until": n.valid_until,
                    "verified_at": n.verified_at,
                }
                for n in sorted(engine.nodes.values(), key=lambda n: n.id)
            ],
            "edges": [{"src": e.src, "dst": e.dst, "type": e.type.value} for e in engine.edges],
            "checkpoint": view["checkpoint"],
            "provenance": audit.to_dict(),
        }

    def get_trace(self, session_id: str) -> Dict[str, Any]:
        self._load(session_id)
        events = self.repository.traces(session_id)
        summary = {
            "events": len(events),
            "tokens": sum(int(e.get("token_usage", 0)) for e in events),
            "latency_ms": round(sum(float(e.get("latency_ms", 0)) for e in events), 2),
            "errors": sum(1 for e in events if e.get("outcome") != "ok"),
            "retries": sum(int(e.get("retries", 0)) for e in events),
        }
        return {"session_id": session_id, "events": events, "summary": summary}

    def get_report(self, session_id: str) -> Dict[str, Any]:
        runtime = self._load(session_id)
        engine = runtime.engine
        row = self.repository.get_session(session_id)
        lines = [
            "# 任务报告",
            f"状态: {runtime.machine.state.value if runtime.machine else 'unknown'}",
        ]
        goal = engine.current_goal()
        goal_text = goal.content if goal is not None else (row.goal if row else "")
        if goal_text:
            lines.append(f"目标: {goal_text}")
        decisions = engine.active_decisions()
        if decisions:
            lines.append("关键决定:")
            lines += [f"  - [{d.id}] {d.content}（{d.reason}）" for d in decisions]
        artifacts = [
            n for n in sorted(engine.nodes.values(), key=lambda n: n.id)
            if n.type is NodeType.ARTIFACT and n.status is NodeStatus.ACTIVE
        ]
        if artifacts:
            lines.append("产出物:")
            lines += [f"  - [{a.id}] {a.content}" for a in artifacts]
        if runtime.machine is not None and runtime.machine.failure_reason:
            lines.append(f"失败原因: {runtime.machine.failure_reason}")
        return {
            "session_id": session_id,
            "state": runtime.machine.state.value if runtime.machine else "unknown",
            "report": "\n".join(lines),
        }

    def restore_task(self, session_id: str) -> Dict[str, Any]:
        """从持久化 checkpoint 重建图和状态机，再返回恢复后的契约视图。"""
        runtime = self._load(session_id)
        checkpoint = self.repository.latest_checkpoint(session_id)
        if checkpoint is None:
            raise StateConflict("没有可恢复的 checkpoint")
        runtime.resume()
        return self.get_state(session_id)

    def replay(self, session_id: str) -> Dict[str, Any]:
        """只凭事件与 checkpoint 回放：重建生命周期、检测损坏、列出 trace。"""
        self._load(session_id)
        events = self.repository.events(session_id)
        states, problems = storage_events.replay_with_problems(events)
        timeline = [
            {"seq": e.seq, "operation": e.operation, "actor": e.actor,
             "payload": dict(e.payload or {})}
            for e in events
            if e.operation != "trace"  # trace 单列，业务时间线保持干净
        ]
        checkpoint = self.repository.latest_checkpoint(session_id)
        return {
            "session_id": session_id,
            "timeline": timeline,
            "states": states,
            "problems": problems,
            "ok": not problems,
            "graph_version": len(self.repository.nodes(session_id)),
            "checkpoint": (
                {
                    "task_state": checkpoint.task_state,
                    "graph_version": checkpoint.graph_version,
                    "pending_role": checkpoint.pending_role,
                    "retry_count": checkpoint.retry_count,
                    "created_at": checkpoint.created_at,
                }
                if checkpoint is not None
                else None
            ),
            "traces": self.repository.traces(session_id),
        }

    # ---------- 内部 ----------

    def _load(self, session_id: str) -> ServiceRuntime:
        if self.repository.get_session(session_id) is None:
            raise NotFound(f"会话不存在：{session_id}")
        runtime = ServiceRuntime(self.repository, session_id)
        runtime.resume()
        if runtime.machine is None:
            runtime.machine = TaskStateMachine()
        return runtime

    def _tracer(self, session_id: str) -> Tracer:
        if self.tracer_factory is not None:
            return self.tracer_factory(session_id)
        return Tracer(
            run_id=session_id,
            sink=lambda event: self.repository.record_trace(session_id, event.to_dict()),
        )

    def _idempotent(self, session_id: str, key: str, operation: str,
                    produce: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        # FastAPI 线程池会并发处理请求；同一运行时实例内先串行化同一会话，
        # 避免两个相同幂等键同时推进状态机。跨进程部署仍应使用单独的请求表/锁服务。
        with self._lock_guard:
            lock = self._session_locks.setdefault(session_id, threading.RLock())
        with lock:
            if key:
                cached = self.repository.get_idempotent_result(session_id, key)
                if cached is not None:
                    replay = dict(cached)
                    replay["replayed"] = True
                    return replay
            result = produce()
            if key:
                result = self.repository.record_idempotent_result(
                    session_id, key, operation, result
                )
            return result

    def _guard(self, request_id: str, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """把内部异常收敛成稳定分类，并把 request_id 带上。"""
        try:
            return fn()
        except RuntimeFailure as exc:
            if not exc.request_id:
                exc.request_id = request_id
            raise
        except Exception as exc:  # 存储/未知故障
            raise StorageFailure(f"{type(exc).__name__}: {exc}", request_id) from exc

    @staticmethod
    def _new_request_id() -> str:
        return f"req-{uuid4().hex[:12]}"
