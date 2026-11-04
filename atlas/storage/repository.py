"""图状态仓储：唯一允许写图的地方。

阶段 4 的纪律：
- 业务层（service / orchestration / 扩展脚本）不碰 ORM 对象，只调这里的方法；
- 每次写入都在一个事务里同时提交业务行与不可变事件；
- `session_id + idempotency_key` 唯一，重放同一请求返回原结果、不新增行；
- 同一个库可被重新打开（进程重启），`rebuild_engine()` 把图还原成 `GraphMemoryEngine`。
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union
from uuid import uuid4

from sqlalchemy import create_engine, event as sa_event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import LEGAL_EDGES, EdgeType, NodeStatus, NodeType
from atlas.storage import events as ev
from atlas.storage import migrations
from atlas.storage.models import (
    LEGACY_WORKFLOW_VERSION,
    AttemptRow,
    Base,
    CheckpointRow,
    ContextPackageRow,
    DependencyRow,
    EdgeRow,
    EventRow,
    NodeRow,
    PlanRow,
    RunMetricRow,
    SessionRow,
    StepRow,
    _now,
)


class Database:
    """一个数据库句柄：建表、开 WAL、产出 session。"""

    def __init__(self, path: Union[str, Path] = ":memory:"):
        if str(path) == ":memory:":
            url = "sqlite://"
        else:
            url = f"sqlite:///{Path(path)}"
        self.url = url
        self.engine: Engine = create_engine(url, future=True)
        if url.startswith("sqlite"):
            sa_event.listen(self.engine, "connect", self._sqlite_pragmas)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )
        Base.metadata.create_all(self.engine)
        # create_all 只建缺失的表；既有表缺列要在这里补（见 storage/migrations.py）。
        self.migration_report = migrations.ensure_schema(self.engine)

    @staticmethod
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # 并发读 + 崩溃恢复
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def close(self) -> None:
        self.engine.dispose()


def open_database(path: Union[str, Path] = ":memory:") -> Database:
    return Database(path)


class GraphRepository:
    """图的持久化入口。"""

    def __init__(self, database: Database):
        self.db = database

    # ---------- 事务 ----------

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        session = self.db.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------- 会话 ----------

    def create_session(
        self,
        session_id: str,
        goal: str = "",
        actor: str = "main",
        *,
        workflow_version: str = LEGACY_WORKFLOW_VERSION,
    ) -> SessionRow:
        with self._tx() as session:
            row = session.get(SessionRow, session_id)
            if row is None:
                row = SessionRow(
                    session_id=session_id,
                    goal=goal,
                    actor=actor,
                    workflow_version=workflow_version,
                )
                session.add(row)
                session.flush()
            return row

    def get_session(self, session_id: str) -> Optional[SessionRow]:
        with self._tx() as session:
            return session.get(SessionRow, session_id)

    def workflow_version(self, session_id: str) -> str:
        """这个会话的控制流版本；库不存在时返回空串（不猜）。"""
        row = self.get_session(session_id)
        return str(getattr(row, "workflow_version", "") or "") if row is not None else ""

    def session_count(self) -> int:
        with self._tx() as session:
            return int(session.scalar(select(func.count()).select_from(SessionRow)) or 0)

    # ---------- 读 ----------

    def nodes(self, session_id: str) -> List[NodeRow]:
        with self._tx() as session:
            return list(
                session.scalars(
                    select(NodeRow)
                    .where(NodeRow.session_id == session_id)
                    .order_by(NodeRow.seq, NodeRow.node_id)
                )
            )

    def edges(self, session_id: str) -> List[EdgeRow]:
        with self._tx() as session:
            return list(
                session.scalars(
                    select(EdgeRow)
                    .where(EdgeRow.session_id == session_id)
                    .order_by(EdgeRow.id)
                )
            )

    def events(self, session_id: str) -> List[EventRow]:
        with self._tx() as session:
            return list(
                session.scalars(
                    select(EventRow)
                    .where(EventRow.session_id == session_id)
                    .order_by(EventRow.seq)
                )
            )

    def metrics(self, session_id: str) -> List[RunMetricRow]:
        with self._tx() as session:
            return list(
                session.scalars(
                    select(RunMetricRow)
                    .where(RunMetricRow.session_id == session_id)
                    .order_by(RunMetricRow.id)
                )
            )

    def latest_checkpoint(self, session_id: str) -> Optional[CheckpointRow]:
        with self._tx() as session:
            return session.scalars(
                select(CheckpointRow)
                .where(CheckpointRow.session_id == session_id)
                .order_by(CheckpointRow.id.desc())
                .limit(1)
            ).first()

    def replay_states(self, session_id: str) -> Dict[str, Dict[str, Any]]:
        """只凭事件重建节点的生命周期状态（审计与 replay 用）。"""
        return ev.replay(self.events(session_id))

    # ---------- 写：节点 ----------

    def add_node(
        self,
        session_id: str,
        type_: Union[NodeType, str],
        content: str,
        actor: str = "main",
        *,
        reason: str = "",
        evidence: str = "",
        note: str = "",
        source: str = "",
        confidence: float = 0.0,
        valid_from: str = "",
        valid_until: str = "",
        verified_at: str = "",
        meta: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> NodeRow:
        node_type = NodeType(type_)
        if not str(content or "").strip():
            raise ValueError(f"{node_type.value} content 不能为空")
        if node_type is NodeType.DECISION and not str(reason or "").strip():
            raise ValueError("decision 必须提供 reason")
        with self._tx() as session:
            self._require_session(session, session_id)
            if idempotency_key:
                existing = session.scalar(
                    select(NodeRow).where(
                        NodeRow.session_id == session_id,
                        NodeRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing
            node_id = self._next_node_id(session, session_id, node_type)
            seq = self._next_seq(session, session_id)
            row = NodeRow(
                session_id=session_id,
                node_id=node_id,
                type=node_type.value,
                content=content,
                actor=actor,
                status=NodeStatus.ACTIVE.value,
                reason=reason,
                evidence=evidence,
                note=note,
                source=source,
                confidence=float(confidence or 0.0),
                valid_from=valid_from,
                valid_until=valid_until,
                verified_at=verified_at,
                meta=meta or {},
                seq=seq,
                idempotency_key=idempotency_key,
                created_at=_now(),
            )
            session.add(row)
            session.flush()
            self._emit_event(
                session,
                session_id,
                ev.ADD_NODE,
                actor,
                ev.node_added(node_id, node_type.value, content, actor),
            )
            self._bump_version(session, session_id)
            return row

    def link(
        self,
        session_id: str,
        src: Any,
        dst: Any,
        type_: Union[EdgeType, str],
        actor: str = "main",
    ) -> EdgeRow:
        edge_type = EdgeType(type_)
        src_id, dst_id = self._as_id(src), self._as_id(dst)
        with self._tx() as session:
            self._require_session(session, session_id)
            src_row = session.get(NodeRow, (session_id, src_id))
            dst_row = session.get(NodeRow, (session_id, dst_id))
            if src_row is None:
                raise KeyError(src_id)
            if dst_row is None:
                raise KeyError(dst_id)
            combo = (edge_type, NodeType(src_row.type), NodeType(dst_row.type))
            if combo not in LEGAL_EDGES:
                raise ValueError(f"非法边：{src_row.type} --{edge_type.value}--> {dst_row.type}")
            existing = self._find_edge(session, session_id, src_id, dst_id, edge_type)
            if existing is not None:
                return existing
            row = EdgeRow(
                session_id=session_id,
                src=src_id,
                dst=dst_id,
                type=edge_type.value,
                created_at=_now(),
            )
            session.add(row)
            session.flush()
            self._emit_event(
                session, session_id, ev.LINK, actor, ev.edge_linked(src_id, dst_id, edge_type)
            )
            self._bump_version(session, session_id)
            return row

    # ---------- 写：生命周期 ----------

    def supersede(
        self, session_id: str, old_id: Any, new_id: Any, reason: str = "", actor: str = "main"
    ) -> None:
        old, new = self._as_id(old_id), self._as_id(new_id)
        with self._tx() as session:
            self._require_session(session, session_id)
            old_row = session.get(NodeRow, (session_id, old))
            new_row = session.get(NodeRow, (session_id, new))
            if old_row is None:
                raise KeyError(old)
            if new_row is None:
                raise KeyError(new)
            combo = (EdgeType.SUPERSEDES, NodeType(old_row.type), NodeType(new_row.type))
            if combo not in LEGAL_EDGES:
                raise ValueError(f"非法作废：{old_row.type} ← {new_row.type}")
            if old_row.status != NodeStatus.ACTIVE.value:
                raise ValueError(f"{old} 当前状态 {old_row.status}，不能再次作废")
            old_row.status = NodeStatus.SUPERSEDED.value
            old_row.superseded_by = new
            old_row.note = reason
            if self._find_edge(session, session_id, new, old, EdgeType.SUPERSEDES) is None:
                session.add(
                    EdgeRow(
                        session_id=session_id,
                        src=new,
                        dst=old,
                        type=EdgeType.SUPERSEDES.value,
                        created_at=_now(),
                    )
                )
            self._emit_event(
                session, session_id, ev.SUPERSEDE, actor, ev.node_superseded(old, new, reason)
            )
            self._bump_version(session, session_id)

    def expire(self, session_id: str, node_id: Any, reason: str = "", actor: str = "main") -> None:
        target = self._as_id(node_id)
        with self._tx() as session:
            self._require_session(session, session_id)
            row = session.get(NodeRow, (session_id, target))
            if row is None:
                raise KeyError(target)
            if row.status != NodeStatus.ACTIVE.value:
                raise ValueError(f"{target} 当前状态 {row.status}，不能标记过期")
            row.status = NodeStatus.EXPIRED.value
            row.note = reason
            self._emit_event(
                session, session_id, ev.EXPIRE, actor, ev.node_expired(target, reason)
            )
            self._bump_version(session, session_id)

    def complete_subtask(self, session_id: str, node_id: Any, actor: str = "main") -> None:
        target = self._as_id(node_id)
        with self._tx() as session:
            self._require_session(session, session_id)
            row = session.get(NodeRow, (session_id, target))
            if row is None:
                raise KeyError(target)
            if NodeType(row.type) not in (NodeType.SUBTASK, NodeType.GOAL):
                raise ValueError(f"{row.type} 没有「完成」语义")
            row.status = NodeStatus.DONE.value
            self._emit_event(
                session, session_id, ev.COMPLETE_SUBTASK, actor, ev.subtask_completed(target)
            )
            self._bump_version(session, session_id)

    # ---------- 写：checkpoint / 指标 ----------

    def save_checkpoint(
        self,
        session_id: str,
        *,
        task_state: str = "planning",
        graph_version: int = 0,
        pending_role: str = "",
        retry_count: int = 0,
        context_digest_version: str = "",
    ) -> CheckpointRow:
        with self._tx() as session:
            self._require_session(session, session_id)
            row = CheckpointRow(
                checkpoint_id=str(uuid4()),
                session_id=session_id,
                task_state=task_state,
                graph_version=graph_version,
                pending_role=pending_role,
                retry_count=retry_count,
                context_digest_version=context_digest_version,
                created_at=_now(),
            )
            session.add(row)
            session.flush()
            return row

    def record_metric(self, session_id: str, **fields: Any) -> RunMetricRow:
        allowed = {
            "step_id",
            "agent_role",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "latency_ms",
            "cost_usd",
            "outcome",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise TypeError(f"未知指标字段：{sorted(unknown)}")
        with self._tx() as session:
            self._require_session(session, session_id)
            row = RunMetricRow(
                metric_id=str(uuid4()), session_id=session_id, created_at=_now(), **fields
            )
            session.add(row)
            session.flush()
            return row

    # ---------- 幂等请求（API 层用） ----------

    def get_idempotent_result(
        self, session_id: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        """同一幂等键已处理过就返回原结果（跨进程有效，因为落在事件表里）。"""
        if not idempotency_key:
            return None
        with self._tx() as session:
            event = session.scalar(
                select(EventRow).where(
                    EventRow.session_id == session_id,
                    EventRow.idempotency_key == idempotency_key,
                )
            )
            if event is None:
                return None
            return (event.payload or {}).get("result")

    def record_idempotent_result(
        self,
        session_id: str,
        idempotency_key: str,
        operation: str,
        result: Dict[str, Any],
        actor: str = "api",
    ) -> Dict[str, Any]:
        """把一次 API 结果绑定到幂等键；已存在则返回原结果，不重复写事件。"""
        if not idempotency_key:
            return result
        with self._tx() as session:
            self._require_session(session, session_id)
            existing = session.scalar(
                select(EventRow).where(
                    EventRow.session_id == session_id,
                    EventRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return (existing.payload or {}).get("result", result)
            self._emit_event(
                session,
                session_id,
                operation,
                actor,
                {"result": result},
                idempotency_key=idempotency_key,
            )
            return result

    # ---------- trace（API 用） ----------

    def record_trace(self, session_id: str, payload: Dict[str, Any], actor: str = "tracer") -> EventRow:
        with self._tx() as session:
            self._require_session(session, session_id)
            return self._emit_event(session, session_id, "trace", actor, payload)

    def traces(self, session_id: str) -> List[Dict[str, Any]]:
        with self._tx() as session:
            rows = session.scalars(
                select(EventRow)
                .where(EventRow.session_id == session_id, EventRow.operation == "trace")
                .order_by(EventRow.seq)
            ).all()
            return [dict(row.payload or {}) for row in rows]

    # ---------- 上下文包（ContextPackage 下钻用） ----------

    def save_context_package(
        self, session_id: str, package: Dict[str, Any]
    ) -> ContextPackageRow:
        """落一次模型调用的上下文包指纹（不存正文，正文可重放）。"""
        with self._tx() as session:
            self._require_session(session, session_id)
            row = ContextPackageRow(
                context_id=str(package.get("context_id") or uuid4()),
                session_id=session_id,
                role=str(package.get("role") or ""),
                state_version=int(package.get("state_version") or 0),
                budget_tokens=int(package.get("budget_tokens") or 0),
                actual_tokens=int(package.get("actual_tokens") or 0),
                digest=str(package.get("digest") or ""),
                tokenizer=str(package.get("tokenizer") or ""),
                sections=dict(package.get("sections") or {}),
                included_refs=list(package.get("included_refs") or []),
                excluded_refs=list(package.get("excluded_refs") or []),
                warnings=list(package.get("warnings") or []),
                degraded=bool(package.get("degraded")),
                created_at=str(package.get("created_at") or _now()),
            )
            session.add(row)
            session.flush()
            return row

    def context_packages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._tx() as session:
            rows = session.scalars(
                select(ContextPackageRow)
                .where(ContextPackageRow.session_id == session_id)
                .order_by(ContextPackageRow.id)
            ).all()
            selected = rows[-limit:] if limit and limit > 0 else rows
            return [
                {
                    "context_id": row.context_id,
                    "role": row.role,
                    "state_version": row.state_version,
                    "budget_tokens": row.budget_tokens,
                    "actual_tokens": row.actual_tokens,
                    "digest": row.digest,
                    "tokenizer": row.tokenizer,
                    "sections": dict(row.sections or {}),
                    "included_refs": list(row.included_refs or []),
                    "excluded_refs": list(row.excluded_refs or []),
                    "warnings": list(row.warnings or []),
                    "degraded": bool(row.degraded),
                    "created_at": row.created_at,
                }
                for row in selected
            ]


    # ---------- 计划 / Step / 依赖（执行状态，与知识图分表） ----------

    def save_plan(
        self,
        session_id: str,
        plan: "PlanSpec",
        *,
        status: str = "proposed",
        validation_report: Optional[Dict[str, Any]] = None,
        generated_by: str = "",
        reason: str = "",
        plan_id: str = "",
        actor: str = "planner",
    ) -> Dict[str, Any]:
        """一份计划 + 它的 Step/Dependency 在**同一事务**里落库并写事件。

        revision 由库内自增（同一 session 内唯一）；plan_id 缺省是 `P<revision>`。
        """
        from atlas.planning.schemas import PlanSpec, StepStatus  # 局部导入避免循环

        if not isinstance(plan, PlanSpec):
            raise TypeError("save_plan 需要 PlanSpec（先用 PlanSpec.from_dict 解析）")
        with self._tx() as session:
            self._require_session(session, session_id)
            revision = self._next_plan_revision(session, session_id)
            pid = plan_id or f"P{revision}"
            session.add(PlanRow(
                session_id=session_id, plan_id=pid, revision=revision,
                parent_plan_id=plan.parent_plan_id or None,
                status=str(status), strategy=plan.strategy, summary=plan.summary,
                spec=plan.to_dict(), validation_report=dict(validation_report or {}),
                validator_version=str((validation_report or {}).get("validator_version", "")),
                generated_by=generated_by or plan.generated_by,
                estimated_budget=dict(plan.estimated_budget),
                reason=reason or plan.reason, created_at=_now(),
            ))
            targets = {item.to_step_id for item in plan.dependencies}
            for spec in plan.steps:
                initial = (StepStatus.BLOCKED if spec.step_id in targets
                           else StepStatus.PENDING)
                session.add(StepRow(
                    session_id=session_id, plan_id=pid, step_id=spec.step_id,
                    ordinal=spec.ordinal, kind=spec.kind.value, title=spec.title,
                    status=initial.value, tool_name=spec.tool_name,
                    max_attempts=spec.max_attempts,
                    spec=spec.to_dict(), created_at=_now(), updated_at=_now(),
                ))
            for dependency in plan.dependencies:
                session.add(DependencyRow(
                    dependency_id=str(uuid4()), session_id=session_id, plan_id=pid,
                    from_step_id=dependency.from_step_id,
                    to_step_id=dependency.to_step_id,
                    kind=dependency.kind.value,
                    binding=dict(dependency.binding or {}),
                    condition=dict(dependency.condition),
                    created_at=_now(),
                ))
            session.flush()
            self._emit_event(
                session, session_id, ev.PLAN_SAVED, actor,
                ev.plan_saved(pid, revision, plan.step_ids,
                              str((validation_report or {}).get("validator_version", "")),
                              str(status)),
            )
            self._bump_version(session, session_id)
        return self.plan_detail(session_id, pid)

    def activate_plan(self, session_id: str, plan_id: str, *,
                      actor: str = "planner") -> Dict[str, Any]:
        """激活一份计划；同一 session 里旧 active plan 一并转 superseded（不可两真源）。"""
        with self._tx() as session:
            self._require_session(session, session_id)
            target = session.get(PlanRow, (session_id, plan_id))
            if target is None:
                raise KeyError(f"计划不存在：{plan_id}")
            if target.status in ("superseded", "rejected"):
                raise ValueError(f"计划 {plan_id} 当前状态 {target.status}，不能激活")
            # 校验没过的计划绝不允许激活：非法环、悬空引用、预算不可行都在这里被挡住。
            report = dict(target.validation_report or {})
            if report and report.get("activatable") is False:
                codes = sorted({item.get("code", "") for item in report.get("issues", [])})
                raise ValueError(f"计划 {plan_id} 未通过校验，不能激活：{codes}")
            previous = session.scalars(
                select(PlanRow).where(
                    PlanRow.session_id == session_id,
                    PlanRow.status == "active",
                    PlanRow.plan_id != plan_id,
                )
            ).all()
            superseded_ids: List[str] = []
            for row in previous:
                row.status = "superseded"
                row.superseded_at = _now()
                superseded_ids.append(row.plan_id)
                self._emit_event(
                    session, session_id, ev.PLAN_SUPERSEDED, actor,
                    ev.plan_superseded(row.plan_id, plan_id, "被新计划取代"),
                )
            target.status = "active"
            target.activated_at = _now()
            self._emit_event(
                session, session_id, ev.PLAN_ACTIVATED, actor,
                ev.plan_activated(plan_id, target.revision,
                                  ",".join(superseded_ids)),
            )
            self._bump_version(session, session_id)
        return self.plan_detail(session_id, plan_id)

    def active_plan_id(self, session_id: str) -> str:
        with self._tx() as session:
            row = session.scalars(
                select(PlanRow).where(
                    PlanRow.session_id == session_id, PlanRow.status == "active"
                )
            ).first()
            return row.plan_id if row is not None else ""

    def plans(self, session_id: str) -> List[Dict[str, Any]]:
        with self._tx() as session:
            rows = session.scalars(
                select(PlanRow).where(PlanRow.session_id == session_id)
                .order_by(PlanRow.revision)
            ).all()
            return [self._plan_row_to_dict(row) for row in rows]

    def plan_detail(self, session_id: str, plan_id: str) -> Dict[str, Any]:
        """一份计划的完整快照：plan + steps + dependencies（恢复与报告都读它）。"""
        with self._tx() as session:
            plan = session.get(PlanRow, (session_id, plan_id))
            if plan is None:
                raise KeyError(f"计划不存在：{plan_id}")
            steps = session.scalars(
                select(StepRow).where(
                    StepRow.session_id == session_id, StepRow.plan_id == plan_id
                ).order_by(StepRow.ordinal, StepRow.step_id)
            ).all()
            dependencies = session.scalars(
                select(DependencyRow).where(
                    DependencyRow.session_id == session_id,
                    DependencyRow.plan_id == plan_id,
                ).order_by(DependencyRow.id)
            ).all()
            return {
                "plan": self._plan_row_to_dict(plan),
                "steps": [self._step_row_to_dict(row) for row in steps],
                "dependencies": [self._dependency_row_to_dict(row) for row in dependencies],
            }

    def step_statuses(self, session_id: str, plan_id: str) -> Dict[str, str]:
        with self._tx() as session:
            rows = session.scalars(
                select(StepRow).where(
                    StepRow.session_id == session_id, StepRow.plan_id == plan_id
                )
            ).all()
            return {row.step_id: row.status for row in rows}

    def set_step_status(
        self, session_id: str, plan_id: str, step_id: str, status: str,
        *, reason: str = "", actor: str = "scheduler",
        expected: Optional[str] = None,
    ) -> bool:
        """按 Step 状态机改状态；非法转移抛 `IllegalStepTransition`。

        `expected` 给定时是乐观锁：库里当前状态与之不符就返回 False（不写）。
        """
        from atlas.orchestration.step_machine import transition

        with self._tx() as session:
            self._require_session(session, session_id)
            row = session.get(StepRow, (session_id, plan_id, step_id))
            if row is None:
                raise KeyError(f"step 不存在：{plan_id}/{step_id}")
            current = row.status
            if expected is not None and current != expected:
                return False
            record = transition(current, status, reason)
            row.status = record.to_status
            row.status_reason = reason
            row.updated_at = record.at
            if record.to_status == "running" and not row.started_at:
                row.started_at = record.at
            if record.to_status in ("done", "failed", "cancelled", "skipped"):
                row.completed_at = record.at
            if record.to_status == "stale":
                row.stale_reason = reason
            self._emit_event(
                session, session_id, ev.STEP_STATUS, actor,
                ev.step_status_changed(plan_id, step_id, record.from_status,
                                       record.to_status, reason),
            )
            self._bump_version(session, session_id)
            return True

    def claim_step(self, session_id: str, plan_id: str, step_id: str,
                   *, actor: str = "scheduler", reason: str = "") -> bool:
        """原子 claim：只有仍是 pending/ready 的那一次能成功（两个 worker 不会同时派发）。

        事务内先把 pending 依合法转移推到 ready，再推到 dispatched；条件更新用
        "读到的那个状态"做守卫，读后被人改过就 rowcount=0 → 本次 claim 失败。
        """
        from atlas.orchestration.step_machine import transition
        from atlas.planning.schemas import StepStatus

        with self._tx() as session:
            self._require_session(session, session_id)
            row = session.get(StepRow, (session_id, plan_id, step_id))
            if row is None:
                raise KeyError(f"step 不存在：{plan_id}/{step_id}")
            observed = row.status
            if observed not in (StepStatus.PENDING.value, StepStatus.READY.value):
                return False
            if observed == StepStatus.PENDING.value:
                transition(observed, StepStatus.READY.value, "依赖与前置条件满足")
            record = transition(StepStatus.READY.value, StepStatus.DISPATCHED.value,
                                reason or "scheduler claim")
            result = session.execute(
                update(StepRow)
                .where(
                    StepRow.session_id == session_id,
                    StepRow.plan_id == plan_id,
                    StepRow.step_id == step_id,
                    StepRow.status == observed,
                )
                .values(status=StepStatus.DISPATCHED.value,
                        status_reason=record.reason, updated_at=record.at)
            )
            if result.rowcount != 1:
                return False
            self._emit_event(
                session, session_id, ev.STEP_CLAIMED, actor,
                ev.step_claimed(plan_id, step_id, observed),
            )
            self._bump_version(session, session_id)
            return True

    def record_attempt(
        self, session_id: str, plan_id: str, step_id: str, *,
        status: str = "created", trigger: str = "initial",
        output: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
        usage: Optional[Dict[str, Any]] = None,
        input_fingerprint: str = "", context_digest: str = "",
        trace_id: str = "", actor: str = "executor",
    ) -> Dict[str, Any]:
        """追加一次 Attempt（不覆盖旧尝试），并同步 Step 的 attempt_count。"""
        with self._tx() as session:
            self._require_session(session, session_id)
            step = session.get(StepRow, (session_id, plan_id, step_id))
            if step is None:
                raise KeyError(f"step 不存在：{plan_id}/{step_id}")
            number = int(step.attempt_count or 0) + 1
            attempt_id = str(uuid4())
            now = _now()
            session.add(AttemptRow(
                attempt_id=attempt_id, session_id=session_id, plan_id=plan_id,
                step_id=step_id, number=number, status=str(status), trigger=str(trigger),
                input_fingerprint=input_fingerprint, context_digest=context_digest,
                output=dict(output or {}), error=dict(error or {}),
                verification=dict(verification or {}), usage=dict(usage or {}),
                trace_id=trace_id, started_at=now,
                finished_at=now if status in ("succeeded", "failed", "interrupted",
                                             "indeterminate") else None,
                created_at=now,
            ))
            step.attempt_count = number
            step.latest_attempt_id = attempt_id
            step.updated_at = now
            self._emit_event(
                session, session_id, ev.ATTEMPT_RECORDED, actor,
                ev.attempt_recorded(plan_id, step_id, attempt_id, number, str(status)),
            )
            self._bump_version(session, session_id)
            return {
                "attempt_id": attempt_id, "number": number, "status": str(status),
                "step_id": step_id, "plan_id": plan_id,
            }

    def update_attempt(
        self, session_id: str, plan_id: str, attempt_id: str, *,
        status: Optional[str] = None,
        output: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None,
        usage: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
        context_digest: Optional[str] = None,
        input_fingerprint: Optional[str] = None,
    ) -> bool:
        """把一次 Attempt 从 running 补成终局；只改给了的字段，历史不覆盖。"""
        with self._tx() as session:
            row = session.scalar(
                select(AttemptRow).where(
                    AttemptRow.session_id == session_id,
                    AttemptRow.plan_id == plan_id,
                    AttemptRow.attempt_id == attempt_id,
                )
            )
            if row is None:
                raise KeyError(f"attempt 不存在：{attempt_id}")
            if status is not None:
                row.status = str(status)
                if str(status) != "running":
                    row.finished_at = row.finished_at or _now()
            if output is not None:
                row.output = dict(output)
            if error is not None:
                row.error = dict(error)
            if usage is not None:
                row.usage = dict(usage)
            if verification is not None:
                row.verification = dict(verification)
            if context_digest is not None:
                row.context_digest = str(context_digest)
            if input_fingerprint is not None:
                row.input_fingerprint = str(input_fingerprint)
            return True

    def attempts(self, session_id: str, plan_id: str, step_id: str = ""
                 ) -> List[Dict[str, Any]]:
        with self._tx() as session:
            query = select(AttemptRow).where(
                AttemptRow.session_id == session_id, AttemptRow.plan_id == plan_id
            )
            if step_id:
                query = query.where(AttemptRow.step_id == step_id)
            rows = session.scalars(query.order_by(AttemptRow.id)).all()
            return [
                {
                    "attempt_id": row.attempt_id, "step_id": row.step_id,
                    "number": row.number, "status": row.status, "trigger": row.trigger,
                    "output": dict(row.output or {}), "error": dict(row.error or {}),
                    "verification": dict(row.verification or {}),
                    "context_digest": row.context_digest,
                    "started_at": row.started_at, "finished_at": row.finished_at,
                }
                for row in rows
            ]

    def mark_downstream_stale(self, session_id: str, plan_id: str,
                              changed_step_ids: List[str], *,
                              reason: str = "上游输入变化") -> List[str]:
        """上游 step 变化后，把已完成的下游打成 stale（§5.5 传播失效）。"""
        from atlas.orchestration.scheduler import stale_after_change

        detail = self.plan_detail(session_id, plan_id)
        statuses = {item["step_id"]: item["status"] for item in detail["steps"]}
        stale_ids = stale_after_change(changed_step_ids, statuses,
                                       [(item["from_step_id"], item["to_step_id"])
                                        for item in detail["dependencies"]])
        for step_id in stale_ids:
            self.set_step_status(session_id, plan_id, step_id, "stale", reason=reason)
        return stale_ids

    # ---------- 镜像（评测接入用） ----------

    def import_engine(self, session_id: str, engine) -> Dict[str, int]:
        """把内存引擎的整张图幂等镜像进库（保留原节点 id 与生命周期）。

        评测记忆服务仍是内存引擎 + JSONL 跑分；这里只做旁路镜像，好让 API / trace
        能查同一批会话。已存在的节点/边直接跳过，重复调用不产生重复行。
        """
        inserted_nodes = 0
        inserted_edges = 0
        inserted_node_ids: set[str] = set()
        with self._tx() as session:
            self._require_session(session, session_id)
            for node in sorted(engine.nodes.values(), key=lambda n: n.id):
                if session.get(NodeRow, (session_id, node.id)) is not None:
                    continue
                node_seq = self._next_node_seq(session, session_id)
                session.add(
                    NodeRow(
                        session_id=session_id,
                        node_id=node.id,
                        type=node.type.value,
                        content=node.content,
                        actor=node.actor,
                        status=node.status.value,
                        superseded_by=node.superseded_by,
                        note=node.note,
                        reason=node.reason,
                        evidence=node.evidence,
                        source=node.source,
                        confidence=float(node.confidence or 0.0),
                        valid_from=node.valid_from,
                        valid_until=node.valid_until,
                        verified_at=node.verified_at,
                        meta=node.meta or {},
                        seq=node_seq,
                        created_at=node.created_at or _now(),
                    )
                )
                session.flush()
                self._emit_event(
                    session, session_id, ev.ADD_NODE, node.actor,
                    ev.node_added(node.id, node.type.value, node.content, node.actor),
                )
                self._bump_version(session, session_id)
                inserted_nodes += 1
                inserted_node_ids.add(node.id)
            # Snapshot rows retain lifecycle state, but replay also needs the
            # corresponding transitions to reconstruct the same state.
            for node in sorted(engine.nodes.values(), key=lambda n: n.id):
                if node.id not in inserted_node_ids:
                    continue
                if node.status is NodeStatus.SUPERSEDED and node.superseded_by:
                    self._emit_event(
                        session, session_id, ev.SUPERSEDE, node.actor,
                        ev.node_superseded(node.id, node.superseded_by, node.note),
                    )
                elif node.status is NodeStatus.EXPIRED:
                    self._emit_event(
                        session, session_id, ev.EXPIRE, node.actor,
                        ev.node_expired(node.id, node.note),
                    )
                elif node.status is NodeStatus.DONE:
                    self._emit_event(
                        session, session_id, ev.COMPLETE_SUBTASK, node.actor,
                        ev.subtask_completed(node.id),
                    )
            for edge in engine.edges:
                if self._find_edge(session, session_id, edge.src, edge.dst, edge.type) is not None:
                    continue
                session.add(
                    EdgeRow(
                        session_id=session_id,
                        src=edge.src,
                        dst=edge.dst,
                        type=edge.type.value,
                        created_at=edge.created_at or _now(),
                    )
                )
                session.flush()
                self._emit_event(
                    session, session_id, ev.LINK, "import",
                    ev.edge_linked(edge.src, edge.dst, edge.type),
                )
                self._bump_version(session, session_id)
                inserted_edges += 1
        return {"nodes": inserted_nodes, "edges": inserted_edges}

    # ---------- 重建 ----------

    def rebuild_engine(self, session_id: str) -> GraphMemoryEngine:
        """把库里的图读回内存引擎（恢复后继续跑官方 runner 用）。"""
        nodes = [
            {
                "id": n.node_id,
                "type": n.type,
                "content": n.content,
                "actor": n.actor,
                "created_at": n.created_at,
                "status": n.status,
                "superseded_by": n.superseded_by,
                "note": n.note,
                "reason": n.reason,
                "evidence": n.evidence,
                "source": n.source,
                "confidence": n.confidence,
                "valid_from": n.valid_from,
                "valid_until": n.valid_until,
                "verified_at": n.verified_at,
                "meta": n.meta or {},
            }
            for n in self.nodes(session_id)
        ]
        edges = [
            {"src": e.src, "dst": e.dst, "type": e.type, "created_at": e.created_at}
            for e in self.edges(session_id)
        ]
        return GraphMemoryEngine.from_snapshot({"nodes": nodes, "edges": edges})

    def export_jsonl(self, session_id: str, path: Union[str, Path]) -> Path:
        """导出为 JSONL：离线调试、diff 与 replay 输入（不再是主存储）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.rebuild_engine(session_id).save_jsonl(target)
        return target

    # ---------- 内部 ----------

    def _emit_event(
        self,
        session: Session,
        session_id: str,
        operation: str,
        actor: str,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> EventRow:
        if idempotency_key:
            existing = session.scalar(
                select(EventRow).where(
                    EventRow.session_id == session_id,
                    EventRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
        row = EventRow(
            event_id=str(uuid4()),
            session_id=session_id,
            seq=self._next_seq(session, session_id),
            actor=actor,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=_now(),
        )
        session.add(row)
        session.flush()
        return row

    def _require_session(self, session: Session, session_id: str) -> SessionRow:
        row = session.get(SessionRow, session_id)
        if row is None:
            raise KeyError(f"会话不存在：{session_id}")
        return row

    @staticmethod
    def _plan_row_to_dict(row: PlanRow) -> Dict[str, Any]:
        return {
            "plan_id": row.plan_id, "revision": row.revision,
            "parent_plan_id": row.parent_plan_id or "",
            "status": row.status, "strategy": row.strategy, "summary": row.summary,
            "spec": dict(row.spec or {}),
            "validation_report": dict(row.validation_report or {}),
            "validator_version": row.validator_version,
            "generated_by": row.generated_by,
            "estimated_budget": dict(row.estimated_budget or {}),
            "reason": row.reason, "created_at": row.created_at,
            "activated_at": row.activated_at or "",
        }

    @staticmethod
    def _step_row_to_dict(row: StepRow) -> Dict[str, Any]:
        return {
            "step_id": row.step_id, "plan_id": row.plan_id, "ordinal": row.ordinal,
            "kind": row.kind, "title": row.title, "status": row.status,
            "tool_name": row.tool_name, "attempt_count": row.attempt_count,
            "max_attempts": row.max_attempts, "status_reason": row.status_reason,
            "stale_reason": row.stale_reason, "failure": dict(row.failure or {}),
            "latest_attempt_id": row.latest_attempt_id or "",
            "input_fingerprint": row.input_fingerprint,
            "spec": dict(row.spec or {}), "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _dependency_row_to_dict(row: DependencyRow) -> Dict[str, Any]:
        return {
            "from_step_id": row.from_step_id, "to_step_id": row.to_step_id,
            "kind": row.kind, "binding": dict(row.binding or {}),
            "condition": dict(row.condition or {}),
        }

    def _next_plan_revision(self, session: Session, session_id: str) -> int:
        current = session.scalar(
            select(func.max(PlanRow.revision)).where(PlanRow.session_id == session_id)
        )
        return int(current or 0) + 1

    def _next_seq(self, session: Session, session_id: str) -> int:
        current = session.scalar(
            select(func.max(EventRow.seq)).where(EventRow.session_id == session_id)
        )
        return int(current or 0) + 1

    def _next_node_id(self, session: Session, session_id: str, type_: NodeType) -> str:
        prefix = type_.id_prefix
        ids = session.scalars(
            select(NodeRow.node_id).where(
                NodeRow.session_id == session_id, NodeRow.node_id.like(f"{prefix}%")
            )
        ).all()
        numbers = [int(i[len(prefix):]) for i in ids if i[len(prefix):].isdigit()]
        return f"{prefix}{max(numbers, default=0) + 1}"

    def _next_node_seq(self, session: Session, session_id: str) -> int:
        current = session.scalar(
            select(func.max(NodeRow.seq)).where(NodeRow.session_id == session_id)
        )
        return int(current or 0) + 1

    def _find_edge(
        self, session: Session, session_id: str, src: str, dst: str, type_: EdgeType
    ) -> Optional[EdgeRow]:
        return session.scalar(
            select(EdgeRow).where(
                EdgeRow.session_id == session_id,
                EdgeRow.src == src,
                EdgeRow.dst == dst,
                EdgeRow.type == type_.value,
            )
        )

    def _bump_version(self, session: Session, session_id: str) -> None:
        row = session.get(SessionRow, session_id)
        if row is not None:
            row.graph_version = int(row.graph_version or 0) + 1
            row.updated_at = _now()

    @staticmethod
    def _as_id(ref: Any) -> str:
        return ref.id if hasattr(ref, "id") else str(ref)
