"""API 层契约：只做 transport 包装，业务字段全部复用 `agent.schemas`。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..agent.schemas import ResearchRequest, ResearchResult


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str = ""
    embedding_model: str = ""
    tools: list[str] = Field(default_factory=list)
    index: dict[str, dict[str, Any]] = Field(default_factory=dict)   # 索引版本/规模，供上游做一致性检查
    n_traces: int = 0
    version: str = "0.1.0"


class RunEnvelope(BaseModel):
    found: bool
    trace: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    error: str
    category: str = ""       # 与 ResearchResult.failure 同一套词表
    detail: str = ""


__all__ = ["ResearchRequest", "ResearchResult", "HealthResponse", "RunEnvelope", "ErrorBody"]
