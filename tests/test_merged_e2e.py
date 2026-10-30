"""端到端任务级评测的**机制**测试：只断言脚手架与判分口径，不把离线结果当效果数字。"""

import json

from integration.merged_e2e import format_table, run_all


def test_merged_e2e_scores_all_scenarios(tmp_path):
    summary = run_all(tmp_path)
    agg = summary["aggregate"]

    assert summary["mode"] == "offline"
    assert "不是效果数字" in summary["note"]
    assert agg["tasks"] == 4
    assert agg["completed"] == agg["tasks"]
    assert agg["research_tasks"] == 3
    assert agg["fact_with_evidence"] == 3
    assert agg["unsupported_facts"] == 0
    assert agg["tool_failure_isolated"] == 1
    assert agg["revision_ok"] == 1
    assert agg["conflict_preserved"] == agg["conflict_tasks"] == 1
    assert agg["recovery_ok"] == 1
    assert agg["duplicate_facts"] == 0

    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["aggregate"]["tasks"] == 4


def test_research_facts_keep_evidence_and_upstream_trace(tmp_path):
    summary = run_all(tmp_path)
    for result in summary["scenarios"]:
        if result.get("research_fact_count"):
            assert result["research_trace_id"], "研究 Fact 必须能下钻到 CompassRAG trace"
            assert result["evidence_chunks"], "研究 Fact 必须带可定位证据"
            assert result["coverage_ok"], f"{result['name']} 证据覆盖未达标"


def test_format_table_flags_offline(tmp_path):
    table = format_table(run_all(tmp_path))
    assert "offline" in table
    assert "不是效果数字" in table
