"""Phase 2 验收：DAG 由持久化状态恢复、只重跑未提交 Attempt、下游 stale。

四条验收（设计文档 §11 Phase 2）：
1. DAG fixture 可由持久化状态恢复 —— 换一个 repository 实例继续跑，不靠内存；
2. 非法环和不可判定 Step 被拒绝 —— validator + 激活闸门 + needs_human；
3. 仅重跑未提交 Attempt —— 崩溃留下的 dispatched 行只重跑它自己；
4. 依赖结果变化会使下游 stale —— mark_downstream_stale。
"""

import pytest

from atlas.orchestration.step_machine import IllegalStepTransition
from atlas.planning.runner import PlanRunner, StepOutcome
from atlas.planning.schemas import PlanSpec
from atlas.planning.validator import validate_plan
from atlas.storage.repository import GraphRepository, open_database

PLAN = {
    "summary": "检索 → 起草 → 核验",
    "steps": [
        {"step_id": "S1", "title": "检索", "kind": "tool", "tool_name": "compassrag",
         "success_criteria": [{"criterion_id": "c1", "kind": "evidence_support",
                                "config": {"min_refs": 1}}]},
        {"step_id": "S2", "title": "起草", "kind": "llm",
         "success_criteria": [{"criterion_id": "c2", "kind": "json_schema",
                                "config": {"required": ["draft"]}}]},
        {"step_id": "S3", "title": "核验", "kind": "verify",
         "success_criteria": [{"criterion_id": "c3", "kind": "test_result",
                                "config": {"field": "ok"}}]},
    ],
    "dependencies": [
        {"from_step_id": "S1", "to_step_id": "S2", "kind": "order"},
        {"from_step_id": "S2", "to_step_id": "S3", "kind": "verify"},
    ],
}


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "plan.db")
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    return GraphRepository(db)


def _save(repo, payload=None, *, session="s1", activate=True):
    repo.create_session(session, goal="写报告")
    spec = PlanSpec.from_dict(payload or PLAN)
    report = validate_plan(spec, allowed_tools={"compassrag"})
    detail = repo.save_plan(session, spec, validation_report=report.to_dict())
    if activate:
        repo.activate_plan(session, detail["plan"]["plan_id"])
    return detail["plan"]["plan_id"]


def _scripted(calls, *, override=None):
    def executor(step, context):
        calls.append(step.step_id)
        if override and step.step_id in override:
            return override[step.step_id](step, context)
        if step.step_id == "S1":
            return StepOutcome(output={"refs": ["c1", "c2"]})
        if step.step_id == "S2":
            return StepOutcome(output={"draft": "草稿"})
        return StepOutcome(output={"ok": True})
    return executor


class TestPlanPersistence:
    def test_save_plan_writes_plan_steps_dependencies_and_events(self, repo):
        plan_id = _save(repo)
        detail = repo.plan_detail("s1", plan_id)
        assert detail["plan"]["status"] == "active"
        assert [item["step_id"] for item in detail["steps"]] == ["S1", "S2", "S3"]
        # 初始状态：有依赖的 blocked，没依赖的 pending
        assert repo.step_statuses("s1", plan_id) == {
            "S1": "pending", "S2": "blocked", "S3": "blocked",
        }
        operations = [event.operation for event in repo.events("s1")]
        assert operations.count("plan_saved") == 1
        assert operations.count("plan_activated") == 1

    def test_new_revision_supersedes_previous(self, repo):
        first = _save(repo)
        spec = PlanSpec.from_dict(PLAN)
        second = repo.save_plan(
            "s1", spec, validation_report=validate_plan(spec).to_dict()
        )["plan"]["plan_id"]
        repo.activate_plan("s1", second)
        plans = {item["plan_id"]: item["status"] for item in repo.plans("s1")}
        assert plans[first] == "superseded"
        assert plans[second] == "active"
        assert repo.active_plan_id("s1") == second

    def test_invalid_plan_cannot_be_activated(self, repo):
        repo.create_session("s1", goal="写报告")
        cyclic = dict(PLAN)
        cyclic["dependencies"] = [
            {"from_step_id": "S1", "to_step_id": "S2"},
            {"from_step_id": "S2", "to_step_id": "S1"},
        ]
        spec = PlanSpec.from_dict(cyclic)
        report = validate_plan(spec)
        assert not report.ok and "CYCLE" in report.error_codes()
        detail = repo.save_plan("s1", spec, validation_report=report.to_dict())
        with pytest.raises(ValueError, match="未通过校验"):
            repo.activate_plan("s1", detail["plan"]["plan_id"])

    def test_rejected_plan_cannot_be_activated_again(self, repo):
        repo.create_session("s1", goal="写报告")
        spec = PlanSpec.from_dict(PLAN)
        detail = repo.save_plan("s1", spec, status="rejected")
        with pytest.raises(ValueError, match="不能激活"):
            repo.activate_plan("s1", detail["plan"]["plan_id"])

    def test_claim_is_atomic_and_one_shot(self, repo):
        plan_id = _save(repo)
        assert repo.claim_step("s1", plan_id, "S1") is True
        assert repo.claim_step("s1", plan_id, "S1") is False
        assert repo.step_statuses("s1", plan_id)["S1"] == "dispatched"

    def test_illegal_status_transition_is_refused(self, repo):
        plan_id = _save(repo)
        repo.claim_step("s1", plan_id, "S1")
        with pytest.raises(IllegalStepTransition):
            repo.set_step_status("s1", plan_id, "S1", "done")


class TestPlanRunner:
    def test_runs_dag_to_completion_in_order(self, repo):
        plan_id = _save(repo)
        calls = []
        report = PlanRunner(repo, "s1", plan_id, _scripted(calls)).run()
        assert report.complete
        assert calls == ["S1", "S2", "S3"]
        assert repo.step_statuses("s1", plan_id) == {
            "S1": "done", "S2": "done", "S3": "done",
        }

    def test_attempts_record_verification_evidence(self, repo):
        plan_id = _save(repo)
        PlanRunner(repo, "s1", plan_id, _scripted([])).run()
        attempts = repo.attempts("s1", plan_id, "S1")
        assert len(attempts) == 1
        assert attempts[0]["verification"]["verified"] is True
        assert attempts[0]["verification"]["criteria"][0]["reason"].startswith("证据")

    def test_failed_criterion_fails_the_step(self, repo):
        plan_id = _save(repo)
        override = {"S1": lambda step, ctx: StepOutcome(output={"refs": []})}
        report = PlanRunner(repo, "s1", plan_id, _scripted([], override=override)).run()
        assert repo.step_statuses("s1", plan_id)["S1"] == "failed"
        assert report.stopped_reason == "failed"

    def test_unverifiable_step_stops_for_human(self, repo):
        plan_id = _save(repo)
        # 判据要求 test_result 字段，但执行器不给 → 判不了 ⇒ needs_human
        override = {"S3": lambda step, ctx: StepOutcome(output={})}
        report = PlanRunner(repo, "s1", plan_id, _scripted([], override=override)).run()
        assert repo.step_statuses("s1", plan_id)["S3"] == "needs_human"
        assert report.stopped_reason == "needs_human"

    def test_second_run_does_not_reexecute_done_steps(self, repo):
        plan_id = _save(repo)
        calls = []
        PlanRunner(repo, "s1", plan_id, _scripted(calls)).run()
        again = PlanRunner(repo, "s1", plan_id, _scripted(calls)).run()
        assert calls == ["S1", "S2", "S3"]
        assert again.executed == []
        assert again.complete


class TestRecovery:
    def test_persisted_plan_resumes_after_restart(self, db, tmp_path):
        """换一个 repository 实例（模拟进程重启）后继续跑完剩下的 Step。"""
        repo = GraphRepository(db)
        plan_id = _save(repo)
        calls = []
        repo.claim_step("s1", plan_id, "S1")
        repo.set_step_status("s1", plan_id, "S1", "running")
        repo.set_step_status("s1", plan_id, "S1", "verifying")
        repo.set_step_status("s1", plan_id, "S1", "done")

        fresh = GraphRepository(db)  # 新实例，内存里没有任何计划对象
        report = PlanRunner(fresh, "s1", plan_id, _scripted(calls)).run()
        assert report.complete
        assert calls == ["S2", "S3"], "已完成的 S1 不应再执行"
        assert repo.attempts("s1", plan_id, "S1") == []

    def test_in_flight_step_is_the_only_one_rerun(self, repo):
        """崩溃留下的 dispatched 行表示"已派发、结果未知"：只重跑它自己。"""
        plan_id = _save(repo)
        repo.claim_step("s1", plan_id, "S1")
        repo.set_step_status("s1", plan_id, "S1", "running")
        repo.set_step_status("s1", plan_id, "S1", "verifying")
        repo.set_step_status("s1", plan_id, "S1", "done")
        repo.set_step_status("s1", plan_id, "S2", "pending", reason="依赖已满足")
        repo.claim_step("s1", plan_id, "S2")  # 派发后就崩了

        calls = []
        report = PlanRunner(repo, "s1", plan_id, _scripted(calls)).run()
        assert report.recovered_in_flight == ["S2"]
        assert calls == ["S2", "S3"]
        assert repo.step_statuses("s1", plan_id) == {
            "S1": "done", "S2": "done", "S3": "done",
        }
        # 重跑用的是新 Attempt（trigger=retry），旧的是历史
        assert [item["trigger"] for item in repo.attempts("s1", plan_id, "S2")] == ["retry"]

    def test_downstream_goes_stale_when_upstream_changes(self, repo):
        plan_id = _save(repo)
        PlanRunner(repo, "s1", plan_id, _scripted([])).run()
        staled = repo.mark_downstream_stale("s1", plan_id, ["S1"], reason="上游证据换了")
        assert staled == ["S2", "S3"]
        statuses = repo.step_statuses("s1", plan_id)
        assert statuses["S2"] == "stale" and statuses["S3"] == "stale"
        assert statuses["S1"] == "done", "上游自己不受影响"

    def test_stale_steps_are_rebound_and_rerun(self, repo):
        plan_id = _save(repo)
        PlanRunner(repo, "s1", plan_id, _scripted([])).run()
        repo.mark_downstream_stale("s1", plan_id, ["S1"])
        # stale → pending 是合法复活路径：重新绑定输入后再跑
        repo.set_step_status("s1", plan_id, "S2", "pending", reason="重新绑定输入")
        repo.set_step_status("s1", plan_id, "S3", "pending", reason="重新绑定输入")
        calls = []
        PlanRunner(repo, "s1", plan_id, _scripted(calls)).run()
        assert calls == ["S2", "S3"]


def test_plan_events_do_not_break_event_replay(repo):
    """执行状态事件与知识图事件同表同 seq：replay 不能把它们当成损坏。"""
    from atlas.storage import events as storage_events

    plan_id = _save(repo)
    PlanRunner(repo, "s1", plan_id, _scripted([])).run()
    states, problems = storage_events.replay_with_problems(repo.events("s1"))
    assert problems == []
    assert states == {}  # 没有知识图节点，纯执行状态不影响投影
