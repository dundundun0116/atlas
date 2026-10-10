"""图状态记忆引擎测试（决策清单 #1：时效字段+决策变迁，六类节点，五类边）。"""

import json

import pytest

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType


@pytest.fixture()
def engine():
    return GraphMemoryEngine()


class TestAddNodes:
    def test_add_goal_assigns_prefixed_id_and_active_status(self, engine):
        goal = engine.add_goal("规划杭州 3 日行")
        assert goal.id == "G1"
        assert goal.type == NodeType.GOAL
        assert goal.status == NodeStatus.ACTIVE
        assert goal.content == "规划杭州 3 日行"
        assert goal.actor == "main"
        assert goal.created_at

    def test_id_prefixes_per_type_and_monotonic(self, engine):
        engine.add_goal("g")
        s = engine.add_subtask("订机票")
        c = engine.add_constraint("预算 5000")
        d = engine.add_decision("住西湖区", reason="离景点近")
        a = engine.add_artifact("行程单.md")
        f = engine.add_fact("杭州东站有地铁 1 号线")
        assert [s.id, c.id, d.id, a.id, f.id] == ["S1", "C1", "D1", "A1", "F1"]
        s2 = engine.add_subtask("订酒店")
        assert s2.id == "S2"

    def test_add_decision_requires_reason(self, engine):
        with pytest.raises(ValueError):
            engine.add_decision("住西湖区")


class TestLink:
    def test_link_records_edge(self, engine):
        goal = engine.add_goal("g")
        sub = engine.add_subtask("s1")
        edge = engine.link(sub, goal, EdgeType.SUBTASK_OF)
        assert (edge.src, edge.dst, edge.type) == (sub.id, goal.id, EdgeType.SUBTASK_OF)

    def test_link_rejects_illegal_type_combo(self, engine):
        goal = engine.add_goal("g")
        c = engine.add_constraint("预算 5000")
        with pytest.raises(ValueError):
            engine.link(goal, c, EdgeType.SUBTASK_OF)  # Goal 不能 SUBTASK_OF 到 Constraint

    def test_depends_on_among_subtasks(self, engine):
        s1 = engine.add_subtask("订机票")
        s3 = engine.add_subtask("定日期")
        engine.link(s1, s3, EdgeType.DEPENDS_ON)
        assert engine.edges[0].type == EdgeType.DEPENDS_ON


class TestSupersede:
    def test_supersede_with_new_content_creates_replacement_and_transitions(self, engine):
        d1 = engine.add_decision("住西湖区", reason="离景点近", evidence="地图")
        d2 = engine.supersede_with(
            d1.id, new_content="住滨江区", reason="展会房价翻倍", evidence="报价单"
        )
        assert d2.id == "D2"
        assert d1.status == NodeStatus.SUPERSEDED
        assert d1.superseded_by == d2.id
        assert d2.status == NodeStatus.ACTIVE
        assert d2.reason == "展会房价翻倍"
        # 决策变迁边：D2 --supersedes--> D1
        assert any(
            e.src == d2.id and e.dst == d1.id and e.type == EdgeType.SUPERSEDES
            for e in engine.edges
        )

    def test_supersede_existing_node(self, engine):
        c1 = engine.add_constraint("日期 5 月 14-16")
        c2 = engine.add_constraint("日期 5 月 21-23")
        engine.supersede(c1.id, by_id=c2.id)
        assert c1.status == NodeStatus.SUPERSEDED
        assert c1.superseded_by == c2.id

    def test_cannot_supersede_twice(self, engine):
        d1 = engine.add_decision("d1", reason="r")
        engine.supersede_with(d1.id, new_content="d2", reason="r2")
        with pytest.raises(ValueError):
            engine.supersede_with(d1.id, new_content="d3", reason="r3")

    def test_supersede_requires_reason(self, engine):
        d1 = engine.add_decision("d1", reason="r")
        with pytest.raises(ValueError):
            engine.supersede_with(d1.id, new_content="d2", reason="")


class TestExpireAndComplete:
    def test_expire_marks_status_and_reason(self, engine):
        c = engine.add_constraint("展会期间不加价")
        engine.expire(c.id, reason="展会结束")
        assert c.status == NodeStatus.EXPIRED
        assert c.note == "展会结束"

    def test_complete_subtask_sets_done_and_open_query_excludes_it(self, engine):
        engine.add_goal("g")
        s1 = engine.add_subtask("定日期")
        s2 = engine.add_subtask("订酒店")
        engine.complete_subtask(s1.id)
        assert s1.status == NodeStatus.DONE
        assert [s.id for s in engine.open_subtasks()] == [s2.id]


class TestQueries:
    def test_active_only_reads(self, engine):
        engine.add_goal("g")
        engine.add_constraint("预算 5000")
        c2 = engine.add_constraint("日期 5 月 14-16")
        engine.expire(c2.id, reason="用户改期")
        d1 = engine.add_decision("d1", reason="r")
        engine.supersede_with(d1.id, new_content="d2", reason="r2")
        assert [c.content for c in engine.active_constraints()] == ["预算 5000"]
        assert [d.content for d in engine.active_decisions()] == ["d2"]

    def test_dependency_order_topological(self, engine):
        s1 = engine.add_subtask("订机票")   # 依赖 s3
        s2 = engine.add_subtask("订酒店")
        s3 = engine.add_subtask("定日期")
        engine.link(s1, s3, EdgeType.DEPENDS_ON)
        ordered = engine.dependency_order([s1.id, s2.id, s3.id])
        assert ordered.index(s3.id) < ordered.index(s1.id)
        assert set(ordered) == {s1.id, s2.id, s3.id}

    def test_dependency_order_rejects_cycle(self, engine):
        s1 = engine.add_subtask("a")
        s2 = engine.add_subtask("b")
        engine.link(s1, s2, EdgeType.DEPENDS_ON)
        engine.link(s2, s1, EdgeType.DEPENDS_ON)
        with pytest.raises(ValueError):
            engine.dependency_order([s1.id, s2.id])

    def test_decision_history_walks_supersede_chain(self, engine):
        d1 = engine.add_decision("住西湖区", reason="离景点近")
        d2 = engine.supersede_with(d1.id, new_content="住滨江区", reason="展会房价翻倍")
        d3 = engine.supersede_with(d2.id, new_content="住萧山区", reason="更便宜")
        history = engine.decision_history(d3.id)
        assert [n.id for n in history] == ["D1", "D2", "D3"]
        assert history[-1].reason == "更便宜"

    def test_hints_contains_superseded_and_expired(self, engine):
        engine.add_goal("g")
        d1 = engine.add_decision("住西湖区", reason="离景点近")
        engine.supersede_with(d1.id, new_content="住滨江区", reason="展会房价翻倍")
        c2 = engine.add_constraint("日期 5 月 14-16")
        engine.expire(c2.id, reason="用户改期")
        hints = engine.hints()
        text = "\n".join(hints)
        assert "住西湖区" in text and "展会房价翻倍" in text  # 被推翻的决定 + 原因
        assert "用户改期" in text

    def test_hints_include_failed_subtasks(self, engine):
        engine.add_goal("g")
        s1 = engine.add_subtask("给出上界")
        engine.complete_subtask(s1.id)
        engine.nodes[s1.id].meta["failed"] = True  # 由服务层按 Judge 结果标记
        engine.nodes[s1.id].note = "Judge: INCORRECT"
        text = "\n".join(engine.hints())
        assert "曾答错" in text and "给出上界" in text


class TestPersistence:
    def test_jsonl_roundtrip(self, engine, tmp_path):
        engine.add_goal("g")
        s1 = engine.add_subtask("定日期", actor="planner")
        s2 = engine.add_subtask("订机票")
        engine.link(s2, s1, EdgeType.DEPENDS_ON)
        d1 = engine.add_decision("住西湖区", reason="r")
        engine.supersede_with(d1.id, new_content="住滨江区", reason="r2")
        path = tmp_path / "graph.jsonl"
        engine.save_jsonl(path)

        loaded = GraphMemoryEngine.load_jsonl(path)
        assert loaded.nodes.keys() == engine.nodes.keys()
        assert len(loaded.edges) == len(engine.edges)
        assert loaded.nodes["D1"].status == NodeStatus.SUPERSEDED
        assert loaded.nodes["D1"].superseded_by == "D2"
        assert loaded.open_subtasks()[0].actor == "planner"

    def test_jsonl_lines_are_valid_json(self, engine, tmp_path):
        engine.add_goal("g")
        path = tmp_path / "graph.jsonl"
        engine.save_jsonl(path)
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert all("kind" in l for l in lines)
