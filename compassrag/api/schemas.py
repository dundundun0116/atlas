"""API 层契约：只做 transport 包装，业务字段全部复用 `agent.schemas`。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..agent.schemas import ResearchRequest, ResearchResult

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class ResearchSubmit(ResearchRequest):
    """提交一次研究任务。`async_mode=True` 时立刻返回 run_id，用 SSE 订阅进度。"""

    async_mode: bool = False


class TraceEvent(BaseModel):
    """SSE 流里的一个事件：轨迹步骤的实时投影。"""

    run_id: str
    seq: int = 0
    kind: str = ""
    ts: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run_id: str
    question: str = ""
    session_id: str = ""
    subtask_id: str = ""
    status: RunStatus = "queued"
    trace_id: str = ""
    answer: str = ""
    stop_reason: str = ""
    failure: str = ""
    confidence: float = 0.0
    rounds: int = 0
    tokens: int = 0
    n_events: int = 0
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    cancelled: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str = ""
    embedding_model: str = ""
    tools: list[str] = Field(default_factory=list)
    index: dict[str, dict[str, Any]] = Field(default_factory=dict)   # 索引版本/规模，供上游做一致性检查
    n_traces: int = 0
    n_runs: int = 0
    version: str = "0.1.0"


class ResearchSession(BaseModel):
    """一次会话（Atlas 的一个子任务上下文）里的运行集合。"""

    session_id: str
    runs: list[RunSummary] = Field(default_factory=list)

    @property
    def latest(self) -> RunSummary | None:
        return self.runs[0] if self.runs else None


class RunEnvelope(BaseModel):
    found: bool
    summary: RunSummary | None = None
    trace: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    error: str
    category: str = ""       # 与 ResearchResult.failure 同一套词表
    detail: str = ""


__all__ = ["ResearchRequest", "ResearchResult", "HealthResponse", "RunEnvelope", "ErrorBody"]
