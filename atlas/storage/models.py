"""阶段 4 持久化模型：session / node / edge / event / checkpoint / run_metric。

设计纪律：
- 图状态写入只经 `atlas.storage.repository`，业务层不直接改这些 ORM 对象；
- 节点、边、supersede 关系与事件在同一事务提交；
- `session_id + idempotency_key` 唯一，重复请求返回原结果；
- 列类型只用 JSON/Text/String/Integer/Float，不写 SQLite 专有类型，保留 PostgreSQL 可迁移性；
- JSONL 降级为导出、调试与 replay 输入，不再是主存储。
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    """所有持久化表的声明式基类。"""


class SessionRow(Base):
    """一次长程任务的会话；graph_version 是状态图版本（每次变更 +1）。"""

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(32), default="main")
    state: Mapped[str] = mapped_column(String(24), default="active")
    graph_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)


class NodeRow(Base):
    """图节点（六类）：生命周期、作废原因与接替指针都留在行上，不静默删除。"""

    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_nodes_idempotency"),
        Index("ix_nodes_session_seq", "session_id", "seq"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(32), default="main")
    status: Mapped[str] = mapped_column(String(16), default="active")
    superseded_by: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    valid_from: Mapped[str] = mapped_column(String(32), default="")
    valid_until: Mapped[str] = mapped_column(String(32), default="")
    verified_at: Mapped[str] = mapped_column(String(32), default="")
    meta: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128))


class EdgeRow(Base):
    """五类边；同一 (session, src, dst, type) 只留一条。"""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("session_id", "src", "dst", "type", name="uq_edges_triple"),
        Index("ix_edges_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    src: Mapped[str] = mapped_column(String(32))
    dst: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class EventRow(Base):
    """不可变事件：只追加，不改不删；`seq` 是会话内的单调序号。"""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_events_idempotency"),
        Index("ix_events_session_seq", "session_id", "seq"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(32), default="main")
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class CheckpointRow(Base):
    """任务检查点：恢复所需的全部业务状态指针。"""

    __tablename__ = "checkpoints"
    __table_args__ = (Index("ix_checkpoints_session", "session_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(36), unique=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    task_state: Mapped[str] = mapped_column(String(24), default="planning")
    graph_version: Mapped[int] = mapped_column(Integer, default=0)
    pending_role: Mapped[str] = mapped_column(String(24), default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    context_digest_version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class RunMetricRow(Base):
    """单步运行指标：模型、token、延迟、成本、结果。"""

    __tablename__ = "run_metrics"
    __table_args__ = (Index("ix_run_metrics_session", "session_id", "step_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metric_id: Mapped[str] = mapped_column(String(36), unique=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    step_id: Mapped[str] = mapped_column(String(64), default="")
    agent_role: Mapped[str] = mapped_column(String(24), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str] = mapped_column(String(24), default="ok")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
