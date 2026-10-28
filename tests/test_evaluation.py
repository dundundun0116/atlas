"""阶段 F：三类长程 fixture、三种记忆策略、故障注入、指标与离线 replay。

验收对应：
- 三类 fixture（多子问题 / 冲突来源 / 需要修订）都有；
- 三种记忆策略同题同预算对照，差异能被指标捕捉；
- 五类故障注入后仍进入可解释终态；
- 指标表同时含质量、可靠性、上下文与成本；失败可回放且能定位到子任务。
"""

import json

import pytest

from atlas.evaluation import (
    FIXTURE_BUILDERS,
    POLICY_NAMES,
    Faults,
    ResearchHarness,
    RunResult,
    aggregate,
    build_policies,
    failing_runs,
    grouped_metrics,
    load_fixtures,
    render_table,
    replay,
    replay_metrics,
    run_matrix,
    save_run,
)
from atlas.evaluation.fixtures import Fixture, Subtask


@pytest.fixture(scope="module")
def matrix():
    return run_matrix(load_fixtures(), build_policies())


@pytest.fixture(scope="module")
def by_policy(matrix):
    return grouped_metrics(matrix, "policy")


class TestFixtures:
    def test_three_families_load_and_validate(self):
        fixtures = load_fixtures()
        assert {f.family for f in fixtures} == {
            "multi_hop", "conflicting_sources", "needs_revision"
        }
        assert all(len(f.subtasks) >= 2 for f in fixtures)

    def test_unknown_family_is_rejected(self):
        with pytest.raises(ValueError):
            load_fixtures(("nope",))

    def test_invalid_fixture_is_rejected(self):
        bad = Fixture(
            task_id="bad", family="multi_hop", goal="g",
            subtasks=(Subtask(subtask_id="S1", question="q", required_fact=""),),
        )
        with pytest.raises(ValueError):
            bad.validate()

    def test_policy_names_are_fixed(self):
        assert POLICY_NAMES == ("full_history", "narrative_summary", "graph_state")
        assert set(build_policies()) == set(POLICY_NAMES)


class TestStrategyContrast:
    def test_matrix_covers_three_fixtures_times_three_policies(self, matrix):
        assert len(matrix) == 3 * 3
        assert {r.policy for r in matrix} == set(POLICY_NAMES)

    def test_graph_and_full_complete_everything(self, by_policy):
        assert by_policy["graph_state"]["completion_rate"] == 1.0
        assert by_policy["full_history"]["completion_rate"] == 1.0
        assert by_policy["narrative_summary"]["completion_rate"] < 1.0

    def test_summary_memory_loses_constraints(self, by_policy):
        assert by_policy["narrative_summary"]["constraint_violation_rate"] > 0
        assert by_policy["graph_state"]["constraint_violation_rate"] == 0
        assert by_policy["full_history"]["constraint_violation_rate"] == 0

    def test_summary_memory_misuses_stale_value(self, by_policy):
        assert by_policy["narrative_summary"]["stale_misuse_rate"] > 0
        assert by_policy["graph_state"]["stale_misuse_rate"] == 0

    def test_graph_state_uses_fewer_context_tokens_on_long_tasks(self, matrix):
        multi = [r for r in matrix if r.family == "multi_hop"]
        graph = next(r.context_tokens for r in multi if r.policy == "graph_state")
        full = next(r.context_tokens for r in multi if r.policy == "full_history")
        assert graph < full


class TestFaultInjection:
    def test_tool_timeout_is_retried_then_recorded(self):
        fixture = load_fixtures(("multi_hop",))[0]
        harness = ResearchHarness(fixture, build_policies()["full_history"])
        result = harness.run(Faults(tool_timeout_at=1))
        step = result.steps[1]
        assert step.error == "timeout"
        assert step.attempts == 2
        assert result.completed is False
        assert result.terminal_state == "failed"

    def test_empty_response_is_retried_once(self):
        fixture = load_fixtures(("multi_hop",))[0]
        harness = ResearchHarness(fixture, build_policies()["full_history"])
        result = harness.run(Faults(empty_response_at=0))
        assert result.steps[0].attempts == 2
        assert result.completed is True

    def test_interrupt_then_resume_recovers(self):
        fixture = load_fixtures(("multi_hop",))[0]
        harness = ResearchHarness(fixture, build_policies()["graph_state"])
        result = harness.run(Faults(interrupt_at=2))
        assert result.interrupted is True
        assert result.recovered is True
        assert result.completed is True
        assert len(result.steps) == len(fixture.subtasks)

    def test_recovery_failure_is_counted_not_hidden(self):
        fixture = load_fixtures(("multi_hop",))[0]
        harness = ResearchHarness(fixture, build_policies()["full_history"])
        result = harness.run(Faults(interrupt_at=1, tool_timeout_at=1))
        assert result.interrupted is True
        assert result.recovered is False
        assert result.completed is False

    def test_duplicate_submission_does_not_duplicate_records(self):
        fixture = load_fixtures(("multi_hop",))[0]
        harness = ResearchHarness(fixture, build_policies()["graph_state"])
        first = harness.run(Faults(duplicate_submit_from=0))
        records_after_first = list(harness.state.records)
        second = harness.run(Faults(duplicate_submit_from=0))
        assert len(second.steps) == len(first.steps)
        assert harness.state.records == records_after_first

    def test_stale_decision_fault_distinguishes_policies(self):
        fixture = load_fixtures(("conflicting_sources",))[0]
        full = ResearchHarness(fixture, build_policies()["full_history"]).run(
            Faults(stale_decision_at=0)
        )
        graph = ResearchHarness(fixture, build_policies()["graph_state"]).run(
            Faults(stale_decision_at=0)
        )
        assert full.steps[0].stale_misuse is True
        assert graph.steps[0].stale_misuse is False


class TestMetrics:
    def test_aggregate_math(self):
        completed = RunResult(task_id="a", family="f", policy="p", completed=True)
        failed = RunResult(task_id="a", family="f", policy="p", completed=False)
        metrics = aggregate([completed, failed])
        assert metrics["n"] == 2
        assert metrics["completion_rate"] == 0.5
        assert metrics["recovery_success_rate"] == 1.0
        assert metrics["interrupted_runs"] == 0

    def test_table_has_quality_reliability_context_cost_columns(self, by_policy):
        table = render_table(by_policy, "按策略")
        for header in ("完成率", "约束违规率", "过期误用率", "恢复成功率",
                       "上下文token", "调用次数", "延迟ms", "成本USD", "n"):
            assert header in table

    def test_failing_runs_points_at_the_step(self, matrix):
        findings = failing_runs(matrix)
        assert findings
        assert {"task_id", "policy", "subtask_id", "violation"} <= set(findings[0])
        assert any(f["stale_misuse"] for f in findings)


class TestReplay:
    def test_replay_reproduces_metrics_and_config(self, tmp_path, matrix):
        path = save_run(
            tmp_path / "run1",
            config={"policy_budget": 800, "fixtures": list(FIXTURE_BUILDERS)},
            runs=matrix,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["config"]["policy_budget"] == 800

        replayed = replay(path)
        assert replayed["metrics"] == aggregate(matrix)
        assert set(replayed["by_policy"]) == set(POLICY_NAMES)
        assert "完成率" in replayed["table"]
        assert replayed["failures"]

    def test_replay_without_model_call_is_stable(self, tmp_path, matrix):
        path = save_run(tmp_path / "run2", config={"n": 9}, runs=matrix)
        assert replay_metrics(path) == replay_metrics(path)

    def test_replay_can_locate_failure_subtask(self, tmp_path):
        fixture = load_fixtures(("conflicting_sources",))[0]
        run = ResearchHarness(fixture, build_policies()["narrative_summary"]).run()
        path = save_run(tmp_path / "run3", config={}, runs=[run])
        failures = replay(path)["failures"]
        assert any(f["subtask_id"] == "S1" and f["stale_misuse"] for f in failures)
