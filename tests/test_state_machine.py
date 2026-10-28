"""阶段 5：显式状态机、角色调用策略与故障恢复。

对应计划阶段 5 的实施项与验收：
- 7 个任务状态与合法转移；非法转移写审计事件并拒绝；
- 角色调用增加 request id、超时、有限重试与 fallback；只重试模型调用；
- Auditor 输出 pass / repair_required / fatal 且必须引用节点或证据；
- 修订最多一次，超限进入 FAILED 并保留原因；
- 故障注入（模型空响应、超时、重复提交、进程中断）后仍收敛到可解释终态；
- LangGraph 只表达控制流，恢复数据仍在 Atlas repository。
"""

import json
import time

import pytest

from atlas.orchestration import OrchestrationError, Orchestrator, TaskState, TaskStateMachine
from atlas.orchestration.policies import (
    AUDIT_OUTCOMES,
    ROLE_POLICIES,
    CallResult,
    RetryPolicy,
    call_with_policy,
    validate_audit,
)
from atlas.orchestration.state_machine import (
    IllegalTransition,
    build_control_flow_graph,
)
from atlas.storage.repository import GraphRepository, open_database


@pytest.fixture()
def machine():
    return TaskStateMachine()


class TestHappyPath:
    def test_reaches_completed_in_three_transitions(self, machine):
        machine.apply("plan_ready", reason="路线已定")
        machine.apply("work_done", reason="草稿完成")
        assert machine.state == TaskState.AUDITING
        machine.apply("audit_pass", reason="证据齐备")
        assert machine.state == TaskState.COMPLETED
        assert machine.is_terminal

    def test_history_records_from_to_trigger_and_reason(self, machine):
        machine.apply("plan_ready", reason="路线已定")
        record = machine.history[-1]
        assert (record["from"], record["to"], record["trigger"]) == ("planning", "executing", "plan_ready")
        assert record["reason"] == "路线已定"


class TestIllegalTransitions:
    def test_work_done_before_planning_is_rejected(self, machine):
        with pytest.raises(IllegalTransition):
            machine.apply("work_done")
        assert machine.state == TaskState.PLANNING
        assert machine.history == []

    def test_illegal_attempt_is_recorded_for_audit(self, machine):
        with pytest.raises(IllegalTransition):
            machine.apply("work_done", reason="越级")
        assert len(machine.violations) == 1
        assert machine.violations[0]["trigger"] == "work_done"
        assert machine.violations[0]["from"] == "planning"
        assert machine.violations[0]["reason"] == "越级"

    def test_terminal_state_rejects_everything(self, machine):
        machine.apply("plan_ready")
        machine.apply("work_done")
        machine.apply("audit_pass")
        for trigger in ("plan_ready", "work_done", "audit_repair", "pause", "resume", "fail"):
            with pytest.raises(IllegalTransition):
                machine.apply(trigger)
        assert machine.state == TaskState.COMPLETED

    def test_unknown_trigger_is_rejected(self, machine):
        with pytest.raises(ValueError):
            machine.apply("teleport")

    def test_duplicate_submission_of_same_trigger_is_rejected(self, machine):
        machine.apply("plan_ready")
        with pytest.raises(IllegalTransition):
            machine.apply("plan_ready")
        assert machine.state == TaskState.EXECUTING
        assert len(machine.history) == 1


class TestPauseResume:
    def test_pause_then_resume_returns_to_previous_state(self, machine):
        machine.apply("plan_ready")
        machine.apply("pause", reason="等用户确认预算")
        assert machine.state == TaskState.PAUSED
        machine.apply("resume")
        assert machine.state == TaskState.EXECUTING

    def test_resume_without_pause_is_rejected(self, machine):
        with pytest.raises(IllegalTransition):
            machine.apply("resume")

    def test_paused_machine_can_fail_out(self, machine):
        machine.apply("pause", reason="端点限流")
        machine.apply("fail", reason="重试额度耗尽")
        assert machine.state == TaskState.FAILED


class TestRepairLoop:
    def test_one_repair_then_pass(self, machine):
        machine.apply("plan_ready")
        machine.apply("work_done")
        machine.apply("audit_repair", reason="常数项错了")
        assert machine.state == TaskState.REPAIRING
        assert machine.repairs == 1
        machine.apply("repair_done")
        machine.apply("audit_pass")
        assert machine.state == TaskState.COMPLETED
        assert machine.repairs == 1

    def test_second_repair_exhausts_budget_and_fails(self, machine):
        machine.apply("plan_ready")
        machine.apply("work_done")
        machine.apply("audit_repair", reason="第一次打回")
        machine.apply("repair_done")
        machine.apply("audit_repair", reason="第二次打回")
        assert machine.state == TaskState.FAILED
        assert "repair" in (machine.failure_reason or "").lower()
        assert machine.repairs == 1  # 预算没被透支

    def test_audit_fatal_fails_directly(self, machine):
        machine.apply("plan_ready")
        machine.apply("work_done")
        machine.apply("audit_fatal", reason="题目无解")
        assert machine.state == TaskState.FAILED
        assert machine.failure_reason == "题目无解"

    def test_repair_budget_is_configurable(self):
        machine = TaskStateMachine(max_repairs=2)
        machine.apply("plan_ready")
        machine.apply("work_done")
        for _ in range(2):
            machine.apply("audit_repair", reason="再改")
            machine.apply("repair_done")
        assert machine.state == TaskState.AUDITING
        assert machine.repairs == 2


class TestCheckpointBridge:
    def test_to_checkpoint_is_restorable(self, machine):
        machine.apply("plan_ready")
        machine.apply("pause", reason="进程要重启")
        snapshot = machine.to_checkpoint()
        restored = TaskStateMachine.from_checkpoint(snapshot)
        restored.apply("resume")
        assert restored.state == TaskState.EXECUTING
        assert restored.to_checkpoint()["state"] == "executing"

    def test_checkpoint_records_repairs_and_paused_origin(self, machine):
        machine.apply("plan_ready")
        machine.apply("work_done")
        machine.apply("audit_repair", reason="打回")
        machine.apply("pause", reason="重启")
        snapshot = machine.to_checkpoint()
        assert snapshot["paused_from"] == "repairing"
        assert snapshot["repairs"] == 1
        assert snapshot["max_repairs"] == 1


class TestAuditValidation:
    def test_three_outcomes_are_defined(self):
        assert AUDIT_OUTCOMES == ("pass", "repair_required", "fatal")

    def test_outcome_without_reference_is_rejected(self):
        with pytest.raises(ValueError):
            validate_audit("pass")
        with pytest.raises(ValueError):
            validate_audit("pass", [])

    def test_outcome_with_reference_is_accepted(self):
        assert validate_audit("pass", ["A1"]) == "pass"
        assert validate_audit("repair_required", ["F1"]) == "repair_required"

    def test_unknown_outcome_is_rejected(self):
        with pytest.raises(ValueError):
            validate_audit("looks_fine", ["A1"])

    def test_llm_verdict_aliases_map_to_outcomes(self):
        from atlas.orchestration.policies import normalize_audit_outcome

        assert normalize_audit_outcome("ok") == "pass"
        assert normalize_audit_outcome("revise") == "repair_required"
        assert normalize_audit_outcome("fatal") == "fatal"
        with pytest.raises(ValueError):
            normalize_audit_outcome("maybe")


class TestRolePolicies:
    def test_three_roles_have_policies(self):
        assert set(ROLE_POLICIES) == {"planner", "worker", "auditor"}
        for policy in ROLE_POLICIES.values():
            assert isinstance(policy, RetryPolicy)
            assert policy.attempts >= 1

    def test_empty_output_is_retried(self):
        replies = ["", "草稿解答"]
        result = call_with_policy(lambda: replies.pop(0), RetryPolicy(attempts=2, timeout_s=1))
        assert isinstance(result, CallResult)
        assert (result.ok, result.value, result.attempts) == (True, "草稿解答", 2)
        assert result.request_id

    def test_exhausted_retries_use_fallback(self):
        result = call_with_policy(
            lambda: "", RetryPolicy(attempts=2, timeout_s=1), fallback="降级草稿"
        )
        assert (result.ok, result.fallback_used) == (False, True)
        assert result.value == "降级草稿"
        assert result.attempts == 2

    def test_timeout_is_retried_then_reported(self):
        def slow():
            time.sleep(0.2)
            return "太晚了"

        result = call_with_policy(slow, RetryPolicy(attempts=2, timeout_s=0.05))
        assert result.ok is False
        assert result.attempts == 2
        assert "timeout" in result.error.lower()

    def test_non_retryable_error_is_not_retried(self):
        calls = []

        def boom():
            calls.append(1)
            raise ValueError("参数非法")

        result = call_with_policy(boom, RetryPolicy(attempts=3, timeout_s=1))
        assert (result.ok, result.attempts, len(calls)) == (False, 1, 1)
        assert "ValueError" in result.error

    def test_state_side_effect_applies_once_after_retry(self):
        """只重试模型调用；成功后才提交一次状态操作。"""
        replies = ["", "", "草稿"]
        applied = []
        machine = TaskStateMachine()
        machine.apply("plan_ready")
        result = call_with_policy(lambda: replies.pop(0), RetryPolicy(attempts=3, timeout_s=1))
        if result.ok:
            applied.append(result.value)
            machine.apply("work_done")
        assert applied == ["草稿"]
        assert len(machine.history) == 2


class TestFaultInjection:
    def test_model_empty_response_fails_task_with_reason(self, machine):
        graph, llm = FakeGraph(), ScriptedLLM([PLAN, "", ""])
        orchestrator = Orchestrator(llm, graph, machine=machine)
        with pytest.raises(OrchestrationError):
            orchestrator.solve("题目：给出上界")
        assert machine.state == TaskState.FAILED
        assert "worker" in (machine.failure_reason or "")

    def test_orchestrator_reaches_completed_through_machine(self, machine):
        graph, llm = FakeGraph(), ScriptedLLM([PLAN, "草稿解答", AUDIT_OK])
        Orchestrator(llm, graph, machine=machine).solve("题目：给出上界")
        assert machine.state == TaskState.COMPLETED
        assert [h["trigger"] for h in machine.history] == ["plan_ready", "work_done", "audit_pass"]

    def test_revise_path_goes_through_repairing(self, machine):
        graph = FakeGraph()
        llm = ScriptedLLM([PLAN, "草稿解答", AUDIT_REVISE, "修订后的解答"])
        Orchestrator(llm, graph, machine=machine).solve("题目：给出上界")
        assert machine.state == TaskState.COMPLETED
        assert "audit_repair" in [h["trigger"] for h in machine.history]
        assert machine.repairs == 1

    def test_resume_from_repository_checkpoint_after_kill(self, tmp_path):
        """进程中断：checkpoint 里存状态机快照，换连接恢复后接着跑。"""
        path = tmp_path / "atlas.db"
        database = open_database(path)
        repo = GraphRepository(database)
        repo.create_session("s1", goal="证明上界")
        machine = TaskStateMachine()
        machine.apply("plan_ready", reason="路线已定")
        repo.add_node("s1", type_="decision", content="用鞅方法", reason="题设依赖", actor="planner")
        repo.save_checkpoint(
            "s1",
            task_state=machine.state.value,
            graph_version=1,
            pending_role="worker",
            retry_count=0,
            context_digest_version=json.dumps(machine.to_checkpoint(), ensure_ascii=False),
        )
        database.close()

        database2 = open_database(path)
        try:
            repo2 = GraphRepository(database2)
            checkpoint = repo2.latest_checkpoint("s1")
            restored = TaskStateMachine.from_checkpoint(
                json.loads(checkpoint.context_digest_version)
            )
            restored.apply("work_done")
            engine = repo2.rebuild_engine("s1")
        finally:
            database2.close()
        assert checkpoint.pending_role == "worker"
        assert restored.state == TaskState.AUDITING
        assert [n.id for n in engine.nodes.values()] == ["D1"]

    def test_database_conflict_leaves_task_resumable(self, machine, tmp_path):
        """库里写失败（重复作废）不吞掉状态机进度，任务仍可继续。"""
        database = open_database(tmp_path / "c.db")
        repo = GraphRepository(database)
        repo.create_session("s1", goal="g")
        repo.add_node("s1", type_="decision", content="A", reason="r")
        repo.add_node("s1", type_="decision", content="B", reason="r")
        repo.supersede("s1", "D1", "D2", reason="改主意")
        with pytest.raises(ValueError):
            repo.supersede("s1", "D1", "D2", reason="重复作废")
        database.close()
        machine.apply("plan_ready")
        assert machine.state == TaskState.EXECUTING


class TestLangGraphControlFlow:
    def test_graph_drives_same_transitions_as_direct_machine(self, machine):
        handlers = {
            "planning": lambda: "plan_ready",
            "executing": lambda: "work_done",
            "auditing": lambda: "audit_pass",
        }
        graph = build_control_flow_graph(machine, handlers)
        final = graph.invoke({"state": machine.state.value}, {"recursion_limit": 20})
        assert final["state"] == "completed"
        assert [h["trigger"] for h in machine.history] == ["plan_ready", "work_done", "audit_pass"]

        direct = TaskStateMachine()
        for handler in handlers.values():
            direct.apply(handler())
        assert direct.state == machine.state
        assert len(direct.history) == len(machine.history)

    def test_graph_stops_on_failure(self, machine):
        handlers = {
            "planning": lambda: "plan_ready",
            "executing": lambda: "fail",
        }
        graph = build_control_flow_graph(machine, handlers)
        final = graph.invoke({"state": machine.state.value}, {"recursion_limit": 20})
        assert final["state"] == "failed"
        assert machine.is_terminal

    def test_graph_can_pause_for_human(self, machine):
        handlers = {
            "planning": lambda: "plan_ready",
            "executing": lambda: "pause",
        }
        graph = build_control_flow_graph(machine, handlers)
        final = graph.invoke({"state": machine.state.value}, {"recursion_limit": 20})
        assert final["state"] == "paused"


class FakeGraph:
    def __init__(self):
        self.notes = []

    def state_view(self, role):
        return f"（{role} 的状态视图）"

    def role_note(self, role, kind, content, reason="", evidence=""):
        self.notes.append({"role": role, "kind": kind, "content": content})
        return {"node_id": f"{role}-{kind}", "kind": kind, "actor": role}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


PLAN = json.dumps({"route": "用 Lemma 2 的鞅版本", "risks": "变量可能不独立", "reason": "题设依赖"})
AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})
AUDIT_REVISE = json.dumps({"verdict": "revise", "issues": "常数项用了 τσ²，应为 τ²σ²"})


class TestAuditHook:
    def test_illegal_transition_reaches_audit_hook(self):
        seen = []
        machine = TaskStateMachine(audit_hook=lambda kind, payload: seen.append((kind, payload)))
        machine.apply("plan_ready", reason="ok")
        with pytest.raises(IllegalTransition):
            machine.apply("plan_ready", reason="重复提交")
        kinds = [k for k, _ in seen]
        assert kinds == ["transition", "illegal_transition"]
        assert seen[-1][1]["trigger"] == "plan_ready"

    def test_runtime_checkpoint_carries_machine_snapshot(self, tmp_path):
        from atlas.service import ServiceRuntime

        path = tmp_path / "rt.db"
        database = open_database(path)
        machine = TaskStateMachine()
        machine.apply("plan_ready")
        runtime = ServiceRuntime(GraphRepository(database), "s1", goal="g", machine=machine)
        runtime.note("planner", "路线", kind="decision", reason="题设")
        saved = runtime.checkpoint(pending_role="worker")
        database.close()

        database2 = open_database(path)
        try:
            resumed = ServiceRuntime(GraphRepository(database2), "s1")
            state = resumed.resume()
        finally:
            database2.close()
        assert saved.task_state == "executing"
        assert state["task_state"] == "executing"
        assert resumed.machine.state == TaskState.EXECUTING
        assert resumed.machine.history[0].trigger == "plan_ready"


class TestAuditorReferences:
    def test_auditor_refs_are_accepted_and_task_completes(self):
        machine = TaskStateMachine()
        graph = FakeGraph()
        audit_with_refs = json.dumps({"verdict": "pass", "issues": "", "refs": ["C1", "D1"]})
        llm = ScriptedLLM([PLAN, "草稿解答", audit_with_refs])
        Orchestrator(llm, graph, machine=machine).solve("题目：给出上界")
        assert machine.state == TaskState.COMPLETED
