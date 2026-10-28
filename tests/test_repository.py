"""阶段 4：关系型持久化、不可变事件与 checkpoint。

对应计划阶段 4 的实施项与验收：
- SQLAlchemy 2 定义 session / node / edge / event / checkpoint / run_metric 表；
- 图状态写入通过 repository，事务保证节点、边、supersede 关系与事件一起提交；
- `session_id + idempotency_key` 唯一约束，重复请求返回原结果；
- checkpoint 保存任务状态、图版本、待执行角色、重试次数、摘要版本；
- SQLite 开 WAL，同时可迁移到 PostgreSQL；
- 事件日志能重建关键状态变化。
"""

import pytest
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType
from atlas.storage.models import Base
from atlas.storage.repository import GraphRepository, open_database


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "atlas.db")
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    return GraphRepository(db)


@pytest.fixture()
def session_id(repo):
    repo.create_session("s1", goal="规划杭州 3 日行")
    return "s1"


def add(repo, session_id="s1", **kw):
    kw.setdefault("content", "节点")
    return repo.add_node(session_id, **kw)


class TestDatabase:
    def test_sqlite_wal_enabled(self, db):
        with db.engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert str(mode).lower() == "wal"

    def test_schema_compiles_for_postgresql(self):
        """数据库层不写 SQLite 专有类型，保证可迁移。"""
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
            assert "CREATE TABLE" in ddl


class TestSession:
    def test_create_session_persists_goal_and_version(self, repo):
        row = repo.create_session("s1", goal="规划杭州 3 日行")
        assert row.session_id == "s1"
        assert row.goal == "规划杭州 3 日行"
        assert row.graph_version == 0

    def test_create_session_is_idempotent(self, repo):
        repo.create_session("s1", goal="g1")
        again = repo.create_session("s1", goal="g1")
        assert again.session_id == "s1"
        assert repo.session_count() == 1


class TestNodeWrites:
    def test_add_node_persists_all_fields(self, repo, session_id):
        node = add(repo, session_id, type_=NodeType.DECISION, content="住西湖区",
                   actor="planner", reason="离景点近", evidence="地图")
        assert (node.node_id, node.type, node.status) == ("D1", "decision", "active")
        stored = repo.nodes(session_id)[0]
        assert (stored.actor, stored.reason, stored.evidence) == ("planner", "离景点近", "地图")
        assert stored.created_at

    def test_node_ids_are_unique_per_session(self, repo):
        repo.create_session("s1", goal="g")
        repo.create_session("s2", goal="g")
        add(repo, "s1", type_=NodeType.GOAL, content="a")
        add(repo, "s2", type_=NodeType.GOAL, content="b")
        assert [n.node_id for n in repo.nodes("s1")] == ["G1"]
        assert [n.node_id for n in repo.nodes("s2")] == ["G1"]

    def test_idempotent_write_returns_original_and_does_not_duplicate(self, repo, session_id):
        first = add(repo, session_id, type_=NodeType.FACT, content="杭州东站有地铁",
                    idempotency_key="req-1")
        again = add(repo, session_id, type_=NodeType.FACT, content="换了内容但同一请求",
                    idempotency_key="req-1")
        assert again.node_id == first.node_id
        assert again.content == "杭州东站有地铁"
        assert len(repo.nodes(session_id)) == 1
        assert [e.operation for e in repo.events(session_id)] == ["add_node"]

    def test_idempotency_key_is_scoped_to_session(self, repo):
        repo.create_session("s1", goal="g")
        repo.create_session("s2", goal="g")
        add(repo, "s1", type_=NodeType.FACT, content="a", idempotency_key="k")
        add(repo, "s2", type_=NodeType.FACT, content="b", idempotency_key="k")
        assert len(repo.nodes("s1")) == len(repo.nodes("s2")) == 1


class TestTransactions:
    def test_link_commits_edge_and_event_together(self, repo, session_id):
        goal = add(repo, session_id, type_=NodeType.GOAL, content="g")
        sub = add(repo, session_id, type_=NodeType.SUBTASK, content="s")
        edge = repo.link(session_id, sub.node_id, goal.node_id, EdgeType.SUBTASK_OF)
        assert (edge.src, edge.dst, edge.type) == ("S1", "G1", "subtask_of")
        assert [e.operation for e in repo.events(session_id)] == ["add_node", "add_node", "link"]

    def test_link_rejects_illegal_combo_without_partial_rows(self, repo, session_id):
        goal = add(repo, session_id, type_=NodeType.GOAL, content="g")
        con = add(repo, session_id, type_=NodeType.CONSTRAINT, content="c")
        with pytest.raises(ValueError):
            repo.link(session_id, goal.node_id, con.node_id, EdgeType.SUBTASK_OF)
        assert repo.edges(session_id) == []
        assert len(repo.events(session_id)) == 2

    def test_duplicate_edge_is_deduplicated(self, repo, session_id):
        goal = add(repo, session_id, type_=NodeType.GOAL, content="g")
        sub = add(repo, session_id, type_=NodeType.SUBTASK, content="s")
        first = repo.link(session_id, sub.node_id, goal.node_id, EdgeType.SUBTASK_OF)
        again = repo.link(session_id, sub.node_id, goal.node_id, EdgeType.SUBTASK_OF)
        assert again.id == first.id
        assert len(repo.edges(session_id)) == 1

    def test_failed_event_write_rolls_back_node(self, repo, session_id, monkeypatch):
        """事件写不进去时，节点也不能留下（事务原子性）。"""
        def boom(*a, **kw):
            raise RuntimeError("event store down")
        monkeypatch.setattr(repo, "_emit_event", boom)
        with pytest.raises(RuntimeError):
            add(repo, session_id, type_=NodeType.FACT, content="x")
        assert repo.nodes(session_id) == []


class TestLifecycle:
    def test_supersede_is_atomic_and_keeps_history(self, repo, session_id):
        old = add(repo, session_id, type_=NodeType.DECISION, content="住西湖区", reason="近")
        new = add(repo, session_id, type_=NodeType.DECISION, content="住滨江", reason="便宜")
        repo.supersede(session_id, old.node_id, new.node_id, reason="预算下调")
        stored = {n.node_id: n for n in repo.nodes(session_id)}
        assert stored["D1"].status == NodeStatus.SUPERSEDED.value
        assert stored["D1"].superseded_by == "D2"
        assert stored["D1"].note == "预算下调"
        assert stored["D2"].status == NodeStatus.ACTIVE.value
        assert [(e.src, e.dst, e.type) for e in repo.edges(session_id)] == [("D2", "D1", "supersedes")]
        assert repo.events(session_id)[-1].operation == "supersede"

    def test_supersede_unknown_node_rolls_back(self, repo, session_id):
        node = add(repo, session_id, type_=NodeType.DECISION, content="d", reason="r")
        with pytest.raises(KeyError):
            repo.supersede(session_id, "D9", node.node_id)
        assert repo.events(session_id)[-1].operation == "add_node"

    def test_expire_and_complete_subtask(self, repo, session_id):
        con = add(repo, session_id, type_=NodeType.CONSTRAINT, content="预算 5000")
        sub = add(repo, session_id, type_=NodeType.SUBTASK, content="订票")
        repo.expire(session_id, con.node_id, reason="预算改为 8000")
        repo.complete_subtask(session_id, sub.node_id)
        stored = {n.node_id: n for n in repo.nodes(session_id)}
        assert stored["C1"].status == NodeStatus.EXPIRED.value
        assert stored["C1"].note == "预算改为 8000"
        assert stored["S1"].status == NodeStatus.DONE.value


class TestCheckpoint:
    def test_checkpoint_roundtrip(self, repo, session_id):
        add(repo, session_id, type_=NodeType.SUBTASK, content="s")
        cp = repo.save_checkpoint(
            session_id,
            task_state="executing",
            graph_version=1,
            pending_role="worker",
            retry_count=2,
            context_digest_version="carry-3",
        )
        latest = repo.latest_checkpoint(session_id)
        assert latest.checkpoint_id == cp.checkpoint_id
        assert (latest.task_state, latest.graph_version) == ("executing", 1)
        assert (latest.pending_role, latest.retry_count) == ("worker", 2)
        assert latest.context_digest_version == "carry-3"

    def test_latest_checkpoint_wins(self, repo, session_id):
        repo.save_checkpoint(session_id, task_state="planning", graph_version=0, pending_role="planner")
        second = repo.save_checkpoint(session_id, task_state="auditing", graph_version=2, pending_role="auditor")
        assert repo.latest_checkpoint(session_id).checkpoint_id == second.checkpoint_id

    def test_resume_from_checkpoint_after_restart(self, tmp_path):
        """模拟进程被杀：换一个连接打开同一个库，图与 checkpoint 都还在。"""
        path = tmp_path / "atlas.db"
        first = open_database(path)
        repository = GraphRepository(first)
        repository.create_session("s1", goal="g")
        repository.add_node("s1", type_=NodeType.SUBTASK, content="订机票")
        repository.save_checkpoint("s1", task_state="executing", graph_version=1, pending_role="worker")
        first.close()

        second = open_database(path)
        try:
            resumed = GraphRepository(second)
            cp = resumed.latest_checkpoint("s1")
            engine = resumed.rebuild_engine("s1")
        finally:
            second.close()
        assert cp.pending_role == "worker"
        assert list(engine.nodes) == ["S1"]
        assert engine.nodes["S1"].content == "订机票"


class TestEvents:
    def test_event_payload_is_immutable_history(self, repo, session_id):
        node = add(repo, session_id, type_=NodeType.FACT, content="Tate 界")
        event = repo.events(session_id)[0]
        assert event.actor == "main"
        assert event.payload["node_id"] == node.node_id
        assert event.payload["type"] == "fact"
        repo.expire(session_id, node.node_id, reason="被推翻")
        assert repo.events(session_id)[0].payload["node_id"] == node.node_id
        assert repo.events(session_id)[-1].payload["reason"] == "被推翻"

    def test_replay_rebuilds_lifecycle_state(self, repo, session_id):
        old = add(repo, session_id, type_=NodeType.DECISION, content="A", reason="r")
        new = add(repo, session_id, type_=NodeType.DECISION, content="B", reason="r")
        repo.supersede(session_id, old.node_id, new.node_id, reason="改主意")
        replayed = repo.replay_states(session_id)
        assert replayed["D1"]["status"] == NodeStatus.SUPERSEDED.value
        assert replayed["D1"]["superseded_by"] == "D2"
        assert replayed["D2"]["status"] == NodeStatus.ACTIVE.value
        stored = {n.node_id: n for n in repo.nodes(session_id)}
        assert replayed["D1"]["status"] == stored["D1"].status

    def test_events_are_ordered_by_seq(self, repo, session_id):
        for i in range(3):
            add(repo, session_id, type_=NodeType.FACT, content=f"f{i}")
        assert [e.seq for e in repo.events(session_id)] == [1, 2, 3]


class TestEngineRebuild:
    def test_rebuild_engine_matches_written_graph(self, repo, session_id):
        goal = add(repo, session_id, type_=NodeType.GOAL, content="g")
        sub = add(repo, session_id, type_=NodeType.SUBTASK, content="s")
        con = add(repo, session_id, type_=NodeType.CONSTRAINT, content="预算 5000")
        d1 = add(repo, session_id, type_=NodeType.DECISION, content="住西湖区", reason="近")
        d2 = add(repo, session_id, type_=NodeType.DECISION, content="住滨江", reason="便宜")
        repo.link(session_id, sub.node_id, goal.node_id, EdgeType.SUBTASK_OF)
        repo.link(session_id, con.node_id, sub.node_id, EdgeType.CONSTRAINS)
        repo.supersede(session_id, d1.node_id, d2.node_id, reason="预算下调")

        engine = repo.rebuild_engine(session_id)
        assert isinstance(engine, GraphMemoryEngine)
        assert [n.id for n in engine.active_constraints()] == ["C1"]
        assert [n.id for n in engine.active_decisions()] == ["D2"]
        assert [n.id for n in engine.decision_history("D2")] == ["D1", "D2"]
        assert engine.nodes["D1"].status == NodeStatus.SUPERSEDED
        assert {e.type for e in engine.edges} == {EdgeType.SUBTASK_OF, EdgeType.CONSTRAINS, EdgeType.SUPERSEDES}
        # 重建后继续追加不撞号
        assert repo.add_node(session_id, type_=NodeType.FACT, content="f").node_id == "F1"

    def test_rebuilt_engine_can_keep_writing_jsonl(self, repo, session_id, tmp_path):
        add(repo, session_id, type_=NodeType.GOAL, content="g")
        engine = repo.rebuild_engine(session_id)
        out = tmp_path / "graph.jsonl"
        engine.save_jsonl(out)
        assert GraphMemoryEngine.load_jsonl(out).nodes["G1"].content == "g"


class TestRunMetric:
    def test_record_metric(self, repo, session_id):
        row = repo.record_metric(
            session_id,
            step_id="st1",
            agent_role="worker",
            model="deepseek-v4.1-flash",
            prompt_tokens=1200,
            completion_tokens=300,
            latency_ms=8100,
            cost_usd=0.0023,
            outcome="ok",
        )
        assert row.prompt_tokens + row.completion_tokens == 1500
        metrics = repo.metrics(session_id)
        assert [m.step_id for m in metrics] == ["st1"]
        assert metrics[0].agent_role == "worker"


class TestServiceRuntime:
    def test_runtime_checkpoints_and_resumes(self, tmp_path):
        from atlas.service import ServiceRuntime

        path = tmp_path / "atlas.db"
        database = open_database(path)
        runtime = ServiceRuntime(GraphRepository(database), "s1", goal="规划杭州 3 日行")
        runtime.note("planner", "路线：订票 → 订酒店", kind="decision", reason="先远后近")
        runtime.note("worker", "草稿一", kind="artifact", idempotency_key="w-1")
        runtime.note("worker", "草稿一", kind="artifact", idempotency_key="w-1")
        runtime.checkpoint(pending_role="auditor", task_state="auditing")
        database.close()

        database2 = open_database(path)
        try:
            resumed = ServiceRuntime(GraphRepository(database2), "s1")
            state = resumed.resume()
        finally:
            database2.close()
        assert [n.id for n in resumed.engine.nodes.values()] == ["D1", "A1"]
        assert state["pending_role"] == "auditor"
        assert state["task_state"] == "auditing"
        assert state["graph_version"] == 2


class TestJsonlExport:
    def test_export_writes_replayable_snapshot(self, repo, session_id, tmp_path):
        goal = add(repo, session_id, type_=NodeType.GOAL, content="g")
        sub = add(repo, session_id, type_=NodeType.SUBTASK, content="s")
        repo.link(session_id, sub.node_id, goal.node_id, EdgeType.SUBTASK_OF)
        out = repo.export_jsonl(session_id, tmp_path / "dump" / "graph.jsonl")
        replayed = GraphMemoryEngine.load_jsonl(out)
        assert list(replayed.nodes) == ["G1", "S1"]
        assert [e.type for e in replayed.edges] == [EdgeType.SUBTASK_OF]

    def test_database_is_source_of_truth_not_jsonl(self, repo, session_id, tmp_path):
        """JSONL 只是导出产物：删掉它不影响库里的图。"""
        add(repo, session_id, type_=NodeType.GOAL, content="g")
        out = repo.export_jsonl(session_id, tmp_path / "graph.jsonl")
        out.unlink()
        assert [n.node_id for n in repo.nodes(session_id)] == ["G1"]
