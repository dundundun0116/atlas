"""环境状态与局部重规划（设计文档 §4.9 / §5.6 / §11 Phase 4）。

这一组守的是三件事：

- **看到的是哪一版**：观测带指纹与时间，指纹由内容算出，过期就等于"不知道"；
- **世界变了只动相关的那部分**：一处对象变了，另一条已完成的分支必须原样有效；
- **每次重规划都能解释**：触发信号、影响范围、复用步骤三样都要说得出。
"""

import json

import pytest

from atlas.environment import (
    EnvironmentSnapshot,
    Observation,
    Precondition,
    check_postcondition,
    check_precondition,
    observations_for,
    preconditions_from_payload,
    resource_fingerprint,
)
from atlas.planning.replan import (
    PlanningDecision,
    apply_replan,
    decide_replan,
    steps_touching_targets,
)
from atlas.planning.schemas import (
    DependencyKind,
    DependencySpec,
    PlanSpec,
    StepKind,
    StepSpec,
)

NOW = 1_700_000_000.0


# ---------------------------------------------------------------- 指纹与观测


class TestFingerprint:
    def test_same_content_same_fingerprint(self):
        assert resource_fingerprint({"a": 1, "b": 2}) == \
            resource_fingerprint({"b": 2, "a": 1}), "键顺序不该影响指纹"

    def test_changed_field_changes_fingerprint(self):
        assert resource_fingerprint({"a": 1}) != resource_fingerprint({"a": 2})

    def test_accepts_plain_strings(self):
        assert resource_fingerprint("abc") != resource_fingerprint("abd")


class TestObservation:
    def test_requires_a_target(self):
        with pytest.raises(ValueError):
            Observation("")

    def test_fingerprint_is_computed_from_value(self):
        observation = Observation("order-1", {"amount": 100})
        assert observation.fingerprint == resource_fingerprint({"amount": 100})

    def test_absent_is_not_the_same_as_unobserved(self):
        # "明确观察到不存在" 与 "没观察过" 是两件事，前置条件判定完全依赖这个区别
        snapshot = EnvironmentSnapshot.of([Observation.absent("order-9")])
        assert snapshot.get("order-9") is not None
        assert snapshot.get("order-9").present is False
        assert snapshot.get("order-8") is None

    def test_freshness_boundary(self):
        observation = Observation("x", 1, observed_at=NOW)
        assert observation.is_fresh(NOW + 10, ttl_s=30) is True
        assert observation.is_fresh(NOW + 31, ttl_s=30) is False
        assert observation.is_fresh(NOW + 10_000, ttl_s=0) is True, "ttl<=0 表示永不过期"

    def test_round_trip(self):
        observation = Observation("x", {"a": 1}, observed_at=NOW, source="lookup")
        assert Observation.from_dict(observation.to_dict()) == observation


class TestSnapshot:
    def _snapshot(self):
        return EnvironmentSnapshot.of([
            Observation("a", {"v": 1}, observed_at=NOW),
            Observation("b", {"v": 2}, observed_at=NOW),
        ])

    def test_observe_returns_a_new_snapshot(self):
        base = self._snapshot()
        updated = base.observe(Observation("a", {"v": 9}, observed_at=NOW))
        assert base.get("a").value == {"v": 1}, "旧快照不能被就地改写"
        assert updated.get("a").value == {"v": 9}

    def test_merge_prefers_the_later(self):
        base = self._snapshot()
        merged = base.merge(EnvironmentSnapshot.of([Observation("b", {"v": 7})]))
        assert merged.get("b").value == {"v": 7}
        assert merged.get("a").value == {"v": 1}

    def test_diff_detects_created_updated_deleted(self):
        before = self._snapshot()
        after = EnvironmentSnapshot.of([
            Observation("a", {"v": 99}, observed_at=NOW),          # updated
            Observation("c", {"v": 3}, observed_at=NOW),           # created
        ])
        changes = {item.target: item.kind for item in before.diff(after)}
        assert changes == {"a": "updated", "b": "deleted", "c": "created"}

    def test_diff_is_empty_when_nothing_changed(self):
        assert self._snapshot().diff(self._snapshot()) == []

    def test_fresh_and_stale_partition(self):
        snapshot = EnvironmentSnapshot.of([
            Observation("fresh", 1, observed_at=NOW),
            Observation("stale", 2, observed_at=NOW - 100),
        ])
        assert snapshot.fresh(now=NOW, ttl_s=30).targets() == ("fresh",)
        assert snapshot.stale_targets(now=NOW, ttl_s=30) == ["stale"]

    def test_round_trip(self):
        base = self._snapshot()
        assert EnvironmentSnapshot.from_dict(base.to_dict()).targets() == base.targets()


# ---------------------------------------------------------------- 前置条件


class TestPrecondition:
    def _snapshot(self, value=None, *, present=True, age=0.0):
        if present:
            return EnvironmentSnapshot.of([
                Observation("order-1", value if value is not None else {"amount": 100},
                            observed_at=NOW - age)])
        return EnvironmentSnapshot.of([
            Observation.absent("order-1", observed_at=NOW - age)])

    def test_explicit_kind_error(self):
        with pytest.raises(ValueError):
            Precondition("order-1", "whatever")

    def test_absent_passes_when_object_missing(self):
        result = check_precondition(Precondition("order-1", "absent"),
                                    self._snapshot(present=False), now=NOW)
        assert result.passed is True

    def test_absent_violated_when_object_exists(self):
        result = check_precondition(Precondition("order-1", "absent"),
                                    self._snapshot(), now=NOW)
        assert result.passed is False
        assert "已经存在" in result.reason

    def test_exists(self):
        assert check_precondition(Precondition("order-1", "exists"),
                                  self._snapshot(), now=NOW).passed is True
        assert check_precondition(Precondition("order-1", "exists"),
                                  self._snapshot(present=False), now=NOW).passed is False

    def test_equals(self):
        assert check_precondition(Precondition("order-1", "equals", {"amount": 100}),
                                  self._snapshot(), now=NOW).passed is True
        assert check_precondition(Precondition("order-1", "equals", {"amount": 1}),
                                  self._snapshot(), now=NOW).passed is False

    def test_contains_only_checks_the_listed_fields(self):
        result = check_precondition(
            Precondition("order-1", "contains", {"amount": 100}),
            self._snapshot({"amount": 100, "currency": "CNY"}), now=NOW)
        assert result.passed is True

    def test_fingerprint_guard(self):
        observation = Observation("order-1", {"amount": 100}, observed_at=NOW)
        plan_time = observation.fingerprint
        assert check_precondition(
            Precondition("order-1", "fingerprint", fingerprint=plan_time),
            EnvironmentSnapshot.of([observation]), now=NOW).passed is True
        changed = EnvironmentSnapshot.of([
            Observation("order-1", {"amount": 999}, observed_at=NOW)])
        assert check_precondition(
            Precondition("order-1", "fingerprint", fingerprint=plan_time),
            changed, now=NOW).passed is False

    def test_missing_observation_is_unknown_not_pass(self):
        result = check_precondition(Precondition("ghost", "absent"),
                                    EnvironmentSnapshot(), now=NOW)
        assert result.passed is None, "没观察过不能当成满足"
        assert result.observe_needed is True

    def test_stale_observation_is_unknown(self):
        result = check_precondition(Precondition("order-1", "absent"),
                                    self._snapshot(present=False, age=999), now=NOW)
        assert result.passed is None
        assert "过期" in result.reason

    def test_no_precondition_passes(self):
        assert check_precondition(None, EnvironmentSnapshot(), now=NOW).passed is True

    def test_payload_parsing_supports_single_and_list(self):
        single = preconditions_from_payload({"precondition": {"target": "a"}})
        assert [item.target for item in single] == ["a"]
        many = preconditions_from_payload(
            {"preconditions": [{"target": "a"}, {"target": "b", "kind": "exists"}]})
        assert [item.target for item in many] == ["a", "b"]
        assert preconditions_from_payload({}) == []


class TestPostcondition:
    def test_subset_match_passes(self):
        result = check_postcondition({"amount": 100},
                                     {"amount": 100, "updated_at": "now"})
        assert result.confirmed is True, "服务端补的字段不该算失败"

    def test_missing_field_fails(self):
        result = check_postcondition({"amount": 100, "currency": "CNY"},
                                     {"amount": 100})
        assert result.confirmed is False
        assert "缺少字段" in result.reason

    def test_mismatched_value_fails(self):
        result = check_postcondition({"amount": 100}, {"amount": 1})
        assert result.confirmed is False

    def test_strict_mode_requires_exact_match(self):
        assert check_postcondition({"a": 1}, {"a": 1, "b": 2}, strict=True).confirmed is False

    def test_non_mapping_observed_fails(self):
        assert check_postcondition({"a": 1}, "nope").confirmed is False

    def test_observations_for_marks_missing_as_absent(self):
        snapshot = observations_for({"a": {"v": 1}, "b": None})
        assert snapshot.get("b").present is False


# ---------------------------------------------------------------- 局部重规划


def _branched_plan() -> PlanSpec:
    """两条互不相干的分支：A（a1→a2）与 B（b1→b2）。"""
    steps = [
        StepSpec(step_id="a1", title="A1", objective="写 a", kind=StepKind.TOOL,
                 ordinal=10, tool_name="w", tool_args_template={"target": "order-a"}),
        StepSpec(step_id="a2", title="A2", objective="确认 a", kind=StepKind.VERIFY,
                 ordinal=20, tool_name="w"),
        StepSpec(step_id="b1", title="B1", objective="写 b", kind=StepKind.TOOL,
                 ordinal=30, tool_name="w", tool_args_template={"target": "order-b"}),
        StepSpec(step_id="b2", title="B2", objective="确认 b", kind=StepKind.VERIFY,
                 ordinal=40, tool_name="w"),
    ]
    dependencies = [
        DependencySpec(from_step_id="a1", to_step_id="a2",
                       kind=DependencyKind.ORDER, binding={}),
        DependencySpec(from_step_id="b1", to_step_id="b2",
                       kind=DependencyKind.ORDER, binding={}),
    ]
    return PlanSpec(steps=steps, dependencies=dependencies, summary="两条分支")


class TestReplan:
    def test_change_only_invalidates_its_own_branch(self):
        plan = _branched_plan()
        statuses = {step_id: "done" for step_id in ("a1", "a2", "b1", "b2")}
        decision = decide_replan(plan, statuses, ["a1"])
        assert decision.stale_steps == ["a2"]
        assert decision.reused_steps == ["b1", "b2"], "不相关的完成分支必须保持有效"
        assert "a1" not in decision.stale_steps, "被改动的自己不算失效"

    def test_unfinished_downstream_is_not_marked_stale(self):
        plan = _branched_plan()
        statuses = {"a1": "done", "a2": "pending", "b1": "done", "b2": "done"}
        decision = decide_replan(plan, statuses, ["a1"])
        assert decision.stale_steps == [], "没完成的步骤本来就要重跑，不需要转 stale"
        assert decision.affected_steps == ["a2"]

    def test_decision_explains_trigger_scope_and_reuse(self):
        plan = _branched_plan()
        statuses = {step_id: "done" for step_id in ("a1", "a2", "b1", "b2")}
        decision = decide_replan(plan, statuses, ["a1"],
                                 changes=[], reason="外部对象 order-a 被改动")
        text = decision.summary()
        assert "外部环境变化" in text
        assert "a1" in text and "a2" in text
        assert "b1" in text and "b2" in text
        assert "order-a" in text
        payload = decision.to_dict()
        assert payload["trigger"] == "environment_changed"
        assert payload["requires_replan"] is True

    def test_unknown_trigger_is_refused(self):
        with pytest.raises(ValueError):
            decide_replan(_branched_plan(), {}, ["a1"], trigger="meteor")

    def test_steps_touching_targets(self):
        plan = _branched_plan()
        assert steps_touching_targets(plan, ["order-a"]) == ["a1"]
        assert steps_touching_targets(plan, ["order-c"]) == []

    def test_no_change_no_work(self):
        plan = _branched_plan()
        statuses = {step_id: "done" for step_id in ("a1", "a2", "b1", "b2")}
        decision = decide_replan(plan, statuses, [])
        assert decision.requires_replan is False
        assert decision.stale_steps == []


class TestApplyReplan:
    def _repository(self, tmp_path):
        from atlas.storage.repository import GraphRepository, open_database

        database = open_database(tmp_path / "replan.db")
        repository = GraphRepository(database)
        repository.create_session("s1", goal="两条分支")
        return repository, database

    def test_stale_is_written_to_storage(self, tmp_path):
        repository, database = self._repository(tmp_path)
        try:
            plan = _branched_plan()
            repository.save_plan("s1", plan, status="active")
            # 先把四步都推到 done（合法的执行路径）
            for step_id in ("a1", "a2", "b1", "b2"):
                if repository.step_statuses("s1", "P1").get(step_id) == "blocked":
                    repository.set_step_status("s1", "P1", step_id, "pending",
                                               reason="依赖已满足")
                repository.claim_step("s1", "P1", step_id)
                repository.set_step_status("s1", "P1", step_id, "running", reason="go")
                repository.set_step_status("s1", "P1", step_id, "verifying", reason="v")
                repository.set_step_status("s1", "P1", step_id, "done", reason="ok")

            statuses = repository.step_statuses("s1", "P1")
            decision = decide_replan(plan, statuses, ["a1"], reason="a 变了")
            result = apply_replan(repository, "s1", "P1", decision)

            assert result["stale_steps"] == ["a2"]
            after = repository.step_statuses("s1", "P1")
            assert after["a2"] == "stale"
            assert after["b1"] == "done" and after["b2"] == "done", "另一条分支不许动"
            assert result["decision"]["trigger"] == "environment_changed"
        finally:
            database.close()
