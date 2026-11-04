"""Step 状态机与确定性调度器（设计文档 §5.2 / §5.3）。

调度器是纯函数：同一快照必须给出同一决定，否则"可复现的恢复"无从谈起。
"""

import pytest

from atlas.orchestration.scheduler import (
    affected_steps,
    data_dependencies,
    dependency_levels,
    plan_tick,
    ready_steps,
    select_next,
    stale_after_change,
    unblockable_steps,
)
from atlas.orchestration.step_machine import (
    IllegalStepTransition,
    allowed_next,
    failure_status_for,
    initial_status,
    is_settled,
    next_status_after_attempt,
    transition,
)
from atlas.planning.schemas import PlanSpec, StepStatus


def _dag():
    return PlanSpec.from_dict({
        "steps": [
            {"step_id": "S1", "ordinal": 0},
            {"step_id": "S2", "ordinal": 1},
            {"step_id": "S3", "ordinal": 2},
            {"step_id": "S4", "ordinal": 3},
        ],
        "dependencies": [
            {"from_step_id": "S1", "to_step_id": "S2", "kind": "data",
             "binding": {"output_name": "a", "input_name": "b"}},
            {"from_step_id": "S2", "to_step_id": "S3"},
            {"from_step_id": "S1", "to_step_id": "S4"},
        ],
    })


class TestStepStateMachine:
    @pytest.mark.parametrize("source,target", [
        ("blocked", "pending"),
        ("pending", "ready"),
        ("ready", "dispatched"),
        ("dispatched", "running"),
        ("running", "verifying"),
        ("verifying", "done"),
        ("done", "stale"),
        ("stale", "pending"),
        ("failed", "ready"),
    ])
    def test_legal_transitions(self, source, target):
        assert transition(source, target).to_status == target

    @pytest.mark.parametrize("source,target", [
        ("ready", "done"),        # 跳过 dispatched/running/verifying 直接完成
        ("pending", "dispatched"),  # 没经 ready 就派发
        ("done", "ready"),        # 完成不能倒回
        ("cancelled", "pending"),  # 终态不可复活
    ])
    def test_illegal_transitions_raise(self, source, target):
        with pytest.raises(IllegalStepTransition):
            transition(source, target)

    def test_done_only_reachable_from_verifying(self):
        sources = [
            status for status in StepStatus
            if "done" in allowed_next(status)
        ]
        assert sources == ["verifying"]

    def test_unknown_status_is_rejected(self):
        with pytest.raises(IllegalStepTransition):
            transition("running", "teleported")

    def test_initial_status_depends_on_dependencies(self):
        assert initial_status(has_unfinished_dependencies=True) is StepStatus.BLOCKED
        assert initial_status(has_unfinished_dependencies=False) is StepStatus.PENDING

    def test_unknown_commit_maps_to_indeterminate(self):
        assert failure_status_for("unknown_commit") is StepStatus.INDETERMINATE
        assert failure_status_for("timeout") is StepStatus.FAILED

    def test_attempt_without_verification_needs_human(self):
        assert next_status_after_attempt("succeeded", True) is StepStatus.DONE
        assert next_status_after_attempt("succeeded", False) is StepStatus.FAILED
        assert next_status_after_attempt("succeeded", None) is StepStatus.NEEDS_HUMAN
        assert next_status_after_attempt("indeterminate", True) is StepStatus.INDETERMINATE

    def test_is_settled(self):
        assert is_settled("done") and is_settled("skipped") and is_settled("cancelled")
        assert not is_settled("needs_human") and not is_settled("stale")


class TestScheduler:
    def test_dependency_levels(self):
        assert dependency_levels(_dag()) == {"S1": 0, "S2": 1, "S3": 2, "S4": 1}

    def test_only_root_is_ready_initially(self):
        statuses = {step: "blocked" for step in ("S1", "S2", "S3", "S4")}
        statuses["S1"] = "pending"
        assert ready_steps(_dag(), statuses) == ["S1"]

    def test_unblock_after_upstream_done(self):
        statuses = {"S1": "done", "S2": "blocked", "S3": "blocked", "S4": "blocked"}
        assert unblockable_steps(_dag(), statuses) == ["S2", "S4"]
        # 顺序稳定：同层按 ordinal
        assert ready_steps(_dag(), statuses) == ["S2", "S4"]

    def test_dependent_step_waits_for_all_parents(self):
        statuses = {"S1": "done", "S2": "done", "S3": "blocked", "S4": "blocked"}
        assert "S3" in ready_steps(_dag(), statuses)

    def test_unknown_precondition_blocks_dispatch(self):
        statuses = {"S1": "done", "S2": "blocked", "S3": "blocked", "S4": "blocked"}
        ready = ready_steps(_dag(), statuses, preconditions={"S2": None, "S4": True})
        assert ready == ["S4"]
        assert ready_steps(_dag(), statuses, preconditions={"S2": False, "S4": True}) == ["S4"]

    def test_select_next_is_serial_and_stable(self):
        decision = select_next(["S2", "S4"])
        assert decision.step_id == "S2"
        assert decision.dispatched

    def test_select_next_respects_pause_and_budget(self):
        assert not select_next(["S2"], paused=True).dispatched
        assert not select_next(["S2"], budget_exhausted=True).dispatched
        assert not select_next([]).dispatched

    def test_empty_ready_explains_what_is_blocked(self):
        plan = _dag()
        statuses = {"S1": "running", "S2": "blocked", "S3": "blocked", "S4": "blocked"}
        decision = select_next([], plan=plan, statuses=statuses)
        assert not decision.dispatched
        assert set(decision.blocked_by) == {"S2", "S3", "S4"}

    def test_plan_tick_reports_unblock_and_choice(self):
        statuses = {"S1": "done", "S2": "blocked", "S3": "blocked", "S4": "blocked"}
        tick = plan_tick(_dag(), statuses)
        assert tick.to_pending == ["S2", "S4"]
        assert tick.decision.step_id == "S2"

    def test_affected_closure_and_stale(self):
        plan = _dag()
        assert affected_steps(["S1"], plan=plan) == {"S2", "S3", "S4"}
        statuses = {"S1": "done", "S2": "done", "S3": "done", "S4": "done"}
        assert stale_after_change(["S1"], statuses, plan=plan) == ["S2", "S3", "S4"]
        # 只打已完成的下游；未完成的不需要 stale
        partial = {"S1": "done", "S2": "done", "S3": "blocked", "S4": "pending"}
        assert stale_after_change(["S1"], partial, plan=plan) == ["S2"]

    def test_data_dependencies_filter(self):
        assert data_dependencies(_dag()) == [("S1", "S2")]
