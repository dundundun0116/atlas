"""结构化 tracing（阶段 E）：回答"这一步为什么发生、花了多少、停在哪"。

字段沿用阶段 0 定的统一运行事件：`run_id / step_id / parent_step_id / agent_role /
tool_name / input_hash / latency_ms / token_usage / outcome`，再补本阶段验收要求的
`model / retries / state_version / stop_reason`。

写 trace 用 `Tracer.record()`；需要落库时分两种：`sink` 回调（同步写事件表）或
`sink=None`（只留内存，测试与本地调试用）。
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

#: 一次运行事件必须能回答的字段（与计划阶段 0 对齐）。
TRACE_FIELDS = (
    "run_id",
    "step_id",
    "parent_step_id",
    "agent_role",
    "tool_name",
    "input_hash",
    "latency_ms",
    "token_usage",
    "outcome",
    "model",
    "retries",
    "state_version",
    "stop_reason",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def hash_input(text: str) -> str:
    """输入指纹：同一输入可关联同一次运行，又不把原文写进 trace。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class TraceEvent:
    trace_id: str
    run_id: str = ""
    step_id: str = ""
    parent_step_id: str = ""
    agent_role: str = ""
    tool_name: str = ""
    model: str = ""
    input_hash: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    state_version: int = 0
    outcome: str = "ok"
    stop_reason: str = ""
    error: str = ""
    created_at: str = ""

    @property
    def token_usage(self) -> int:
        return int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["token_usage"] = self.token_usage
        return payload


@dataclass
class Tracer:
    """每次调用记一条事件；可选 sink 落库（例如写进 repository 的事件表）。"""

    run_id: str = ""
    sink: Optional[Callable[[TraceEvent], None]] = None
    events: List[TraceEvent] = field(default_factory=list)

    def record(
        self,
        *,
        agent_role: str = "",
        tool_name: str = "",
        model: str = "",
        step_id: str = "",
        parent_step_id: str = "",
        input_text: str = "",
        input_hash: str = "",
        latency_ms: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        retries: int = 0,
        state_version: int = 0,
        outcome: str = "ok",
        stop_reason: str = "",
        error: str = "",
        trace_id: str = "",
    ) -> TraceEvent:
        event = TraceEvent(
            trace_id=trace_id or f"trace-{uuid4().hex[:12]}",
            run_id=self.run_id,
            step_id=step_id,
            parent_step_id=parent_step_id,
            agent_role=agent_role,
            tool_name=tool_name,
            model=model,
            input_hash=input_hash or hash_input(input_text),
            latency_ms=float(latency_ms or 0.0),
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            retries=int(retries or 0),
            state_version=int(state_version or 0),
            outcome=outcome,
            stop_reason=stop_reason,
            error=error,
            created_at=_now(),
        )
        self.events.append(event)
        if self.sink is not None:
            self.sink(event)
        return event

    def summary(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "events": len(self.events),
            "tokens": sum(e.token_usage for e in self.events),
            "latency_ms": round(sum(e.latency_ms for e in self.events), 2),
            "errors": sum(1 for e in self.events if e.outcome != "ok"),
            "retries": sum(e.retries for e in self.events),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary(), "events": [e.to_dict() for e in self.events]}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
