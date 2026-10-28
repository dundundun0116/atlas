"""阶段 D：provenance 六字段、组装前治理与确定性组装。

对应计划阶段 D 的任务与验收：
- Fact/Constraint/Decision/Artifact 带 source/actor/confidence/valid_from/valid_until/verified_at；
- 组装前检查互斥约束、过期决策、未验证产出；
- 记录实际 token、被过滤节点与原因；
- 提取失败有限重试 + 规则摘要降级；
- 同一图快照 + 同一预算下组装结果可复现。
"""

import json

import pytest

from atlas.context.assembler import ContextAssembler
from atlas.context.carryover import CarryOverManager
from atlas.context.extractor import LLMCarryOverExtractor
from atlas.context.provenance import (
    PROVENANCE_FIELDS,
    audit_nodes,
    is_stale,
    is_unverified,
    missing_provenance,
    risk_lines,
)
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import NodeStatus, NodeType
from atlas.storage.repository import GraphRepository, open_database


def count_words(text: str) -> int:
    return max(1, len(text.split()))


def make_assembler(engine, budget_shares=None):
    return ContextAssembler(engine, count_words, shares=budget_shares)


def clean_engine():
    """全部节点都带 source → 治理报告为空（用来验证"干净时不加噪音"）。"""
    eng = GraphMemoryEngine()
    eng.add_goal("给出 L 的上界", source="user")
    eng.add_constraint("预算 5000", source="user")
    eng.add_decision("用鞅方法", reason="题设依赖", source="planner", confidence=0.8)
    eng.add_fact("Lemma 2 给出鞅版本界", source="paper:2312.05134", confidence=0.9)
    eng.add_artifact("行程单.md", source="worker", verified_at="2026-10-28T00:00:00")
    return eng


class TestSchemaRoundTrip:
    def test_node_round_trips_all_provenance_fields(self):
        eng = GraphMemoryEngine()
        node = eng.add_fact("事实", source="paper:1", confidence=0.7,
                            valid_from="2026-10-01", valid_until="2026-12-31",
                            verified_at="2026-10-28")
        restored = type(node).from_dict(node.to_dict())
        for name in PROVENANCE_FIELDS:
            assert getattr(restored, name) == getattr(node, name)
        assert set(PROVENANCE_FIELDS) == {
            "source", "actor", "confidence", "valid_from", "valid_until", "verified_at"
        }

    def test_stale_and_unverified_helpers(self):
        eng = GraphMemoryEngine()
        old = eng.add_constraint("预算 5000", valid_until="2000-01-01")
        fresh = eng.add_constraint("预算 8000", valid_until="2099-01-01")
        artifact = eng.add_artifact("草稿.md")
        verified = eng.add_artifact("报告.md", verified_at="2026-10-28")
        assert is_stale(old) is True
        assert is_stale(fresh) is False
        assert is_unverified(artifact) is True
        assert is_unverified(verified) is False
        assert missing_provenance(artifact) is True


class TestAudit:
    def test_clean_graph_produces_no_warnings(self):
        eng = clean_engine()
        audit = audit_nodes(eng.nodes.values())
        assert audit.is_empty is True
        assert risk_lines(audit) == []

    def test_stale_nodes_are_filtered_and_reported(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", valid_until="2000-01-01")
        audit = audit_nodes(eng.nodes.values())
        stale_ids = [item["node_id"] for item in audit.stale]
        assert stale_ids == ["C2"]
        assert "过期" in risk_lines(audit)[1]

    def test_conflicting_constraints_are_flagged_not_resolved(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", meta={"key": "budget"})
        eng.add_constraint("预算 8000", source="user2", meta={"key": "budget"})
        audit = audit_nodes(eng.nodes.values())
        assert len(audit.conflicts) == 1
        assert audit.conflicts[0].key == "budget"
        assert set(audit.conflicts[0].node_ids) == {"C2", "C3"}
        # 两条都还在图里，没有被静默挑一条
        assert {n.status for n in eng.active_constraints()} == {NodeStatus.ACTIVE}

    def test_missing_source_is_reported(self):
        eng = GraphMemoryEngine()
        eng.add_fact("没有来源的事实")
        audit = audit_nodes(eng.nodes.values())
        assert "F1" in audit.missing_source


class TestAssemblyGovernance:
    def test_stale_constraint_does_not_enter_plan_and_audit_still_passes(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", valid_until="2000-01-01")
        result = make_assembler(eng).assemble([], [], 4000)
        # 不进"活跃约束"区，但在风险段说明为什么被挡（不是静默丢弃）
        assert "  - [C2] 预算 5000" not in result.text
        assert "[C2] 已过期" in result.text
        assert result.audit_ok is True
        assert result.filtered == {"C2": "valid_until=2000-01-01 已过期"}
        assert "风险与待确认" in result.text

    def test_unverified_artifact_is_marked(self):
        eng = clean_engine()
        eng.add_artifact("草稿.md", source="worker")  # 无 verified_at
        result = make_assembler(eng).assemble([], [], 4000)
        assert "[A2] 草稿.md（未验证）" in result.text
        assert "[A1] 行程单.md（未验证）" not in result.text

    def test_conflict_is_visible_in_plan(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", meta={"key": "budget"})
        eng.add_constraint("预算 8000", source="user2", meta={"key": "budget"})
        text = make_assembler(eng).assemble([], [], 4000).text
        assert "约束冲突（key=budget）" in text

    def test_stats_record_tokens_filters_and_provenance(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", valid_until="2000-01-01")
        result = make_assembler(eng).assemble(["摘要"], ["原始记录"], 4000)
        assert result.actual_tokens > 0
        assert result.provenance["stale"][0]["node_id"] == "C2"
        assert result.zone_tokens["plan"] > 0

    def test_same_snapshot_same_budget_is_deterministic(self):
        eng = clean_engine()
        eng.add_constraint("预算 5000", source="user", meta={"key": "budget"})
        eng.add_constraint("预算 8000", source="user2", meta={"key": "budget"})
        eng.add_fact("另一条事实")
        assembler = make_assembler(eng)
        first = assembler.assemble(["摘要一"], ["记录一", "记录二"], 3000).text
        second = assembler.assemble(["摘要一"], ["记录一", "记录二"], 3000).text
        assert first == second

    def test_source_and_confidence_are_annotated(self):
        eng = GraphMemoryEngine()
        eng.add_fact("主引理", source="paper:2312.05134", confidence=0.9)
        text = make_assembler(eng).assemble([], [], 4000).text
        assert "来源: paper:2312.05134" in text
        assert "置信度 0.90" in text


class TestExtractionDegradation:
    def test_degraded_extraction_marks_summary(self):
        replies = ["不是 JSON", "还是不是 JSON"]

        class Flaky:
            def __call__(self, _prompt):
                return replies.pop(0)

        engine = GraphMemoryEngine()
        extractor = LLMCarryOverExtractor(Flaky())
        manager = CarryOverManager(engine, count_words)
        summaries = manager.execute(["## Task: 证明上界\n## Judge: CORRECT"], extractor)
        assert "[降级·规则摘要]" in summaries[0]
        assert summaries[0].startswith("[步骤 1–1]")
        assert engine.nodes == {}


class TestRepositoryProvenance:
    def test_provenance_is_persisted_and_rebuilt(self, tmp_path):
        database = open_database(tmp_path / "p.db")
        try:
            repo = GraphRepository(database)
            repo.create_session("s1", goal="g")
            repo.add_node(
                "s1", type_="fact", content="事实", source="paper:1", confidence=0.7,
                valid_from="2026-10-01", valid_until="2026-12-31", verified_at="2026-10-28",
            )
            engine = repo.rebuild_engine("s1")
        finally:
            database.close()
        node = engine.nodes["F1"]
        assert (node.source, node.confidence) == ("paper:1", 0.7)
        assert (node.valid_from, node.valid_until) == ("2026-10-01", "2026-12-31")
        assert node.verified_at == "2026-10-28"


class TestResearchProvenanceClosure:
    """D6 欠账：研究结论进图时带真 provenance（而不是只把 JSON 塞进 evidence）。"""

    def test_research_fact_carries_source_and_confidence(self):
        from atlas.orchestration import Orchestrator
        from atlas.tools import LocalResearchTool

        class ProvenanceGraph:
            def __init__(self):
                self.notes = []

            def state_view(self, role):
                return f"（{role}）"

            def role_note(self, role, kind, content, reason="", evidence="",
                          source="", confidence=0.0):
                self.notes.append({"role": role, "content": content,
                                   "source": source, "confidence": confidence})
                return {"node_id": "F9"}

        class LLM:
            def __init__(self, replies):
                self.replies = list(replies)

            def __call__(self, _prompt):
                return self.replies.pop(0)

        plan = json.dumps({"route": "鞅方法", "risks": "", "reason": "题设"})
        audit = json.dumps({"verdict": "ok", "issues": ""})
        graph = ProvenanceGraph()
        tool = LocalResearchTool(
            lambda req: {"answer": "L ≤ 2τσ²", "confidence": 0.84,
                         "evidence": [{"source": "paper:2312.05134", "quote": "Thm 3"}]}
        )
        Orchestrator(LLM([plan, "草稿", audit]), graph, research_tool=tool).solve(
            "题目：给出上界", session_id="s1", subtask_id="S1"
        )
        fact = [n for n in graph.notes if n["role"] == "research"][0]
        assert fact["source"] == "paper:2312.05134"
        assert fact["confidence"] == 0.84
