"""研究工具协议（阶段 A）：Atlas 通过稳定接口调用外部检索能力。

Atlas 不实现 BM25/向量/实体图检索，只定义请求、响应与错误类型；具体检索由
CompassRAG（本地函数或 HTTP 服务）提供。这样上层 Planner/Worker/Auditor 不随
检索实现变化，也让"工具失败"成为可分类、可注入的一等公民。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol
from uuid import uuid4

#: 工具错误分类：上层只按这五类做决策（重试 / 降级 / 终止）。
ERROR_KINDS = (
    "timeout",
    "invalid_request",
    "no_evidence",
    "budget_exhausted",
    "upstream_error",
)


def new_trace_id() -> str:
    return f"trace-{uuid4().hex[:12]}"


def build_idempotency_key(session_id: str, subtask_id: str, attempt: int = 1) -> str:
    return f"{session_id or 'session'}:{subtask_id or 'subtask'}:attempt-{attempt}"


class ResearchToolError(RuntimeError):
    """工具失败：`kind` 决定上层策略，消息里保留可读原因。"""

    def __init__(self, kind: str, message: str = "", *, trace_id: str = ""):
        if kind not in ERROR_KINDS:
            raise ValueError(f"未知工具错误类型：{kind}（可选 {ERROR_KINDS}）")
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message
        self.trace_id = trace_id


@dataclass(frozen=True)
class EvidenceItem:
    """一条证据：来源 + 定位 + 引文 + 分数。"""

    source: str
    chunk_id: str = ""
    quote: str = ""
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "chunk_id": self.chunk_id,
            "quote": self.quote,
            "score": float(self.score or 0.0),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "EvidenceItem":
        if not isinstance(payload, dict):
            raise ValueError(f"evidence 项必须是对象，收到 {type(payload).__name__}")
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("evidence 缺少 source")
        return cls(
            source=source,
            chunk_id=str(payload.get("chunk_id") or ""),
            quote=str(payload.get("quote") or ""),
            score=float(payload.get("score") or 0.0),
        )


@dataclass(frozen=True)
class ResearchRequest:
    """一次检索请求：带预算、轮次上限、幂等键与 trace_id。"""

    session_id: str
    subtask_id: str
    query: str
    budget_tokens: int = 4000
    max_rounds: int = 2
    idempotency_key: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        for name in ("session_id", "subtask_id", "query"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"ResearchRequest.{name} 不能为空")
        if self.budget_tokens <= 0:
            raise ValueError("budget_tokens 必须为正")
        if self.max_rounds <= 0:
            raise ValueError("max_rounds 必须为正")
        # 缺省自动补齐：调用方不传也能拿到可追踪的 id 与幂等键
        if not self.trace_id:
            object.__setattr__(self, "trace_id", new_trace_id())
        if not self.idempotency_key:
            object.__setattr__(
                self,
                "idempotency_key",
                build_idempotency_key(self.session_id, self.subtask_id),
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "subtask_id": self.subtask_id,
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "max_rounds": self.max_rounds,
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
        }


@dataclass
class ResearchResult:
    """一次检索的结论：答案 + 证据 + 置信度 + 停止原因。"""

    answer: str
    evidence: List[EvidenceItem] = field(default_factory=list)
    confidence: float = 0.0
    retrieval_rounds: int = 0
    stop_reason: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence or 0.0)))

    @property
    def usable(self) -> bool:
        """没有答案就没有可用结论（只有证据也不算）。"""
        return bool(str(self.answer or "").strip())

    def evidence_json(self) -> str:
        return json.dumps([item.to_dict() for item in self.evidence], ensure_ascii=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "evidence": [item.to_dict() for item in self.evidence],
            "confidence": self.confidence,
            "retrieval_rounds": self.retrieval_rounds,
            "stop_reason": self.stop_reason,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ResearchResult":
        if not isinstance(payload, dict):
            raise ValueError(f"工具响应必须是对象，收到 {type(payload).__name__}")
        raw_items = payload.get("evidence") or []
        if not isinstance(raw_items, list):
            raise ValueError("evidence 必须是数组")
        return cls(
            answer=str(payload.get("answer") or ""),
            evidence=[EvidenceItem.from_dict(item) for item in raw_items],
            confidence=payload.get("confidence") or 0.0,
            retrieval_rounds=int(payload.get("retrieval_rounds") or 0),
            stop_reason=str(payload.get("stop_reason") or ""),
            trace_id=str(payload.get("trace_id") or ""),
        )


class ResearchTool(Protocol):
    """研究工具协议：任何实现只要能 research 一次请求即可。"""

    def research(self, request: ResearchRequest) -> ResearchResult:  # pragma: no cover
        ...
