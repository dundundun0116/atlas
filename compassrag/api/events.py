"""运行注册表与事件总线（Task 1/2）。

边界：事件是 `AgentState.step` 的**实时投影**，不另建状态——轨迹仍是唯一真源。
这里只解决三件事：运行状态机（queued/running/completed/failed/cancelled）、
事件历史 + 订阅队列（SSE 用）、以及协作式取消位。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_END = object()


@dataclass
class RunRecord:
    run_id: str
    question: str
    session_id: str = ""
    subtask_id: str = ""
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    trace_id: str = ""
    answer: str = ""
    stop_reason: str = ""
    failure: str = ""
    confidence: float = 0.0
    rounds: int = 0
    tokens: int = 0
    n_events: int = 0
    error: str = ""
    cancelled: bool = False
    result: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "question": self.question, "status": self.status,
            "session_id": self.session_id, "subtask_id": self.subtask_id,
            "trace_id": self.trace_id, "answer": self.answer,
            "stop_reason": self.stop_reason, "failure": self.failure,
            "confidence": self.confidence, "rounds": self.rounds,
            "tokens": self.tokens, "n_events": self.n_events,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "cancelled": self.cancelled,
        }


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def create(self, run_id: str, question: str, *, session_id: str = "",
               subtask_id: str = "") -> RunRecord:
        with self._lock:
            rec = RunRecord(run_id=run_id, question=question,
                            session_id=session_id, subtask_id=subtask_id)
            self._runs[run_id] = rec
            return rec

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def update(self, run_id: str, **fields: Any) -> RunRecord | None:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None:
                return None
            for k, v in fields.items():
                setattr(rec, k, v)
            return rec

    def request_cancel(self, run_id: str) -> bool:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is None or rec.is_terminal:
                return False
            rec.cancelled = True
            return True

    def is_cancelled(self, run_id: str) -> bool:
        rec = self.get(run_id)
        return bool(rec and rec.cancelled)

    def recent(self, limit: int = 50, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
            if session_id:
                runs = [r for r in runs if r.session_id == session_id]
        return [r.as_summary() for r in runs[:limit]]


class EventPublisher:
    """每个 run 一条事件流；订阅者各拿一个队列，客户端断开不影响 Agent 继续跑。"""

    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._subs: dict[str, list[queue.Queue]] = {}
        self._closed: set[str] = set()
        self._lock = threading.Lock()

    def publish(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            events = self._events.setdefault(run_id, [])
            seq = len(events)
            item = {"run_id": run_id, "seq": seq, **event}
            events.append(item)
            subs = list(self._subs.get(run_id, []))
        for q in subs:
            q.put(item)
        return item

    def history(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events.get(run_id, []))

    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(run_id, [])
            if q in subs:
                subs.remove(q)

    def close(self, run_id: str) -> None:
        with self._lock:
            self._closed.add(run_id)
            subs = list(self._subs.get(run_id, []))
        for q in subs:
            q.put(_END)

    def is_closed(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._closed

    @staticmethod
    def end_marker():
        return _END


__all__ = ["RunRecord", "RunRegistry", "EventPublisher", "TERMINAL_STATUSES"]
