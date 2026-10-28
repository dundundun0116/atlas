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

from sqlalchemy import create_engine, event as sa_event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import LEGAL_EDGES, EdgeType, NodeStatus, NodeType
from atlas.storage import events as ev
from atlas.storage.models import (
    Base,
    CheckpointRow,
    EdgeRow,
    EventRow,
    NodeRow,
    RunMetricRow,
    SessionRow,
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
        self, session_id: str, goal: str = "", actor: str = "main"
    ) -> SessionRow:
        with self._tx() as session:
            row = session.get(SessionRow, session_id)
            if row is None:
                row = SessionRow(session_id=session_id, goal=goal, actor=actor)
                session.add(row)
                session.flush()
            return row

    def get_session(self, session_id: str) -> Optional[SessionRow]:
        with self._tx() as session:
            return session.get(SessionRow, session_id)

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
