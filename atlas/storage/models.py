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
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 现有 `/orchestrate` 路径的显式工作流版本。它不是 DAG：只有一个固定的
#: Planner→Worker→Auditor 控制流，没有持久化的 Plan/Step/Dependency。恢复旧
#: checkpoint 时按这个值区分「legacy 流程」与将来的 DAG 流程，不伪造计划数据。
LEGACY_WORKFLOW_VERSION = "legacy_research_v1"


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
    # 这个会话跑的是哪套控制流。恢复时据此决定能不能按 DAG 语义继续。
    workflow_version: Mapped[str] = mapped_column(
        String(32), default=LEGACY_WORKFLOW_VERSION
    )
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
    # 状态机快照是完整 JSON，长度随历史增长，不应受 64 字符限制。
    context_digest_version: Mapped[str] = mapped_column(Text, default="")
    # §4.13：恢复要知道"哪些 Step 还没收敛、哪些工具调用在途、预算还剩多少"
    pending_step_ids: Mapped[list] = mapped_column(JSON, default=list)
    in_flight_tool_call_ids: Mapped[list] = mapped_column(JSON, default=list)
    budget_snapshot: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
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


class ContextPackageRow(Base):
    """一次模型调用实际看到的上下文包（可复盘「它当时看到了什么」）。

    只存 digest、分区 token 数、包含/排除的引用和降级原因，**不存上下文正文**：
    正文可由「同一图快照 + 同一预算」重放得到，存全文会把状态库变成日志库。
    """

    __tablename__ = "context_packages"
    __table_args__ = (
        Index("ix_context_packages_session", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context_id: Mapped[str] = mapped_column(String(36), unique=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(32), default="")
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    budget_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_tokens: Mapped[int] = mapped_column(Integer, default=0)
    digest: Mapped[str] = mapped_column(String(64), default="")
    tokenizer: Mapped[str] = mapped_column(String(48), default="")
    sections: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    included_refs: Mapped[list] = mapped_column(JSON, default=list)
    excluded_refs: Mapped[list] = mapped_column(JSON, default=list)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


# --------------------------------------------------------------------------- #
# 计划作为一等状态（设计文档 §4.3–4.7 / §9.1）
#
# 与知识图分表：`plans/steps/dependencies/attempts` 是**执行状态**，
# `nodes/edges` 是**知识状态**。两者可以同库，但字段、写入 API、生命周期必须分开——
# 执行依赖绝不复用 `EdgeRow`（§4.6 明确要求）。
# --------------------------------------------------------------------------- #


class PlanRow(Base):
    """一份计划 revision；同一 session 同一时刻只能有一个 active plan。"""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("session_id", "revision", name="uq_plans_revision"),
        Index("ix_plans_session_status", "session_id", "status"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parent_plan_id: Mapped[Optional[str]] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    strategy: Mapped[str] = mapped_column(String(16), default="dag")
    summary: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_report: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    validator_version: Mapped[str] = mapped_column(String(32), default="")
    generated_by: Mapped[str] = mapped_column(String(64), default="model")
    estimated_budget: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    activated_at: Mapped[Optional[str]] = mapped_column(String(32))
    superseded_at: Mapped[Optional[str]] = mapped_column(String(32))


class StepRow(Base):
    """一个 Step 的**当前**状态；spec 里的静态定义不随状态变化。"""

    __tablename__ = "steps"
    __table_args__ = (
        Index("ix_steps_session_plan_status", "session_id", "plan_id", "status"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="llm")
    title: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="blocked")
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    latest_attempt_id: Mapped[Optional[str]] = mapped_column(String(36))
    status_reason: Mapped[str] = mapped_column(Text, default="")
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    failure: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    spec: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)
    started_at: Mapped[Optional[str]] = mapped_column(String(32))
    completed_at: Mapped[Optional[str]] = mapped_column(String(32))


class DependencyRow(Base):
    """执行依赖（order / data / verify）；一条 `data` 边必须带 binding。"""

    __tablename__ = "dependencies"
    __table_args__ = (
        UniqueConstraint("session_id", "plan_id", "from_step_id", "to_step_id", "kind",
                         name="uq_dependencies_edge"),
        Index("ix_dependencies_session_plan", "session_id", "plan_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dependency_id: Mapped[str] = mapped_column(String(36), unique=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    plan_id: Mapped[str] = mapped_column(String(36))
    from_step_id: Mapped[str] = mapped_column(String(64))
    to_step_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="order")
    binding: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    condition: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class AttemptRow(Base):
    """一次 Step 执行尝试；重试**追加新行**，不覆盖旧尝试（§4.7）。"""

    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("session_id", "plan_id", "step_id", "number", name="uq_attempts_number"),
        Index("ix_attempts_step", "session_id", "plan_id", "step_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(String(36), unique=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    plan_id: Mapped[str] = mapped_column(String(36))
    step_id: Mapped[str] = mapped_column(String(64))
    number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="created")
    trigger: Mapped[str] = mapped_column(String(16), default="initial")
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    context_digest: Mapped[str] = mapped_column(String(64), default="")
    output: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    verification: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    usage: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[Optional[str]] = mapped_column(String(32))
    finished_at: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)


class ToolCallRow(Base):
    """一次外部工具调用：先记意图、再派发、再落四档结论（§4.8）。

    `(session_id, idempotency_key)` 唯一——同一个逻辑操作只会有一行，
    重试复用同一行而不是新增（这正是"不重复副作用"能让下游去重的前提）。
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_tool_calls_idem"),
        Index("ix_tool_calls_step", "session_id", "plan_id", "step_id"),
    )

    tool_call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.session_id", ondelete="CASCADE")
    )
    plan_id: Mapped[str] = mapped_column(String(36), default="")
    step_id: Mapped[str] = mapped_column(String(64), default="")
    attempt_id: Mapped[str] = mapped_column(String(36), default="")
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    tool_version: Mapped[str] = mapped_column(String(32), default="")
    effect_class: Mapped[str] = mapped_column(String(24), default="read_only")
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    outcome: Mapped[Optional[str]] = mapped_column(String(24))
    canonical_args: Mapped[str] = mapped_column(Text, default="")
    args_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str] = mapped_column(String(160), default="")
    external_operation_id: Mapped[str] = mapped_column(String(128), default="")
    result: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    reconcile_attempts: Mapped[int] = mapped_column(Integer, default=0)
    compensation_for: Mapped[Optional[str]] = mapped_column(String(36))
    dispatched_at: Mapped[Optional[str]] = mapped_column(String(32))
    settled_at: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), default=_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now)
