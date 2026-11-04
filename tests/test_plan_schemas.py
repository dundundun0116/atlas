"""计划作为一等状态：解析与可执行性校验（设计文档 §4.3–4.6 / §5.4）。

校验器是"计划能不能跑"的唯一闸门，所以每条错误码都要有对应用例——
否则代码里写着闸门，实际放行的还是坏计划。
"""

import pytest

from atlas.planning.schemas import PlanSchemaError, PlanSpec, StepStatus
from atlas.planning.validator import validate_plan


def _plan(**overrides):
    payload = {
        "summary": "检索 → 起草 → 核验",
        "steps": [
            {"step_id": "S1", "title": "检索", "kind": "tool", "tool_name": "compassrag",
             "outputs": [{"name": "evidence", "schema": {"type": "array"}}],
             "success_criteria": [{"criterion_id": "c1", "kind": "evidence_support",
                                    "config": {"min_refs": 1}}]},
            {"step_id": "S2", "title": "起草", "kind": "llm",
             "inputs": [{"name": "ev", "source_kind": "step_output",
                          "expected_schema": {"type": "array"}}],
             "outputs": [{"name": "draft", "schema": {"type": "object"}}],
             "success_criteria": [{"criterion_id": "c2", "kind": "json_schema",
                                    "config": {"required": ["draft"]}}]},
            {"step_id": "S3", "title": "核验", "kind": "verify",
             "success_criteria": [{"criterion_id": "c3", "kind": "test_result",
                                    "config": {"field": "ok"}}]},
        ],
        "dependencies": [
            {"from_step_id": "S1", "to_step_id": "S2", "kind": "data",
             "binding": {"output_name": "evidence", "input_name": "ev"}},
            {"from_step_id": "S2", "to_step_id": "S3", "kind": "verify"},
        ],
    }
    payload.update(overrides)
    return payload


class TestPlanParsing:
    def test_parses_steps_bindings_and_dependencies(self):
        spec = PlanSpec.from_dict(_plan())
        assert spec.step_ids == ["S1", "S2", "S3"]
        assert spec.step("S2").input_named("ev").source_kind == "step_output"
        assert spec.dependencies[0].binding == {"output_name": "evidence", "input_name": "ev"}
        assert spec.to_dict()["steps"][0]["kind"] == "tool"

    def test_rejects_missing_steps(self):
        with pytest.raises(PlanSchemaError):
            PlanSpec.from_dict({"summary": "空计划"})

    def test_rejects_unknown_step_kind(self):
        with pytest.raises(PlanSchemaError):
            PlanSpec.from_dict(_plan(steps=[{"step_id": "S1", "kind": "telepathy"}]))

    def test_rejects_unknown_input_source_kind(self):
        with pytest.raises(PlanSchemaError):
            PlanSpec.from_dict(_plan(steps=[{
                "step_id": "S1",
                "inputs": [{"name": "x", "source_kind": "vibes"}],
            }]))

    def test_rejects_bad_max_attempts(self):
        with pytest.raises(PlanSchemaError):
            PlanSpec.from_dict(_plan(steps=[{"step_id": "S1", "max_attempts": 0}]))


class TestValidatorGates:
    def test_happy_path_is_activatable(self):
        report = validate_plan(_plan(), allowed_tools={"compassrag"})
        assert report.ok and report.activatable
        assert report.topological_order == ["S1", "S2", "S3"]
        assert report.human_required == []

    def test_schema_error_becomes_issue_not_exception(self):
        report = validate_plan({"steps": []})
        assert not report.ok
        assert report.error_codes() == ["SCHEMA_INVALID"]

    def test_duplicate_step_ids_rejected(self):
        report = validate_plan(_plan(steps=[
            {"step_id": "S1"}, {"step_id": "S1"},
        ]))
        assert "DUPLICATE_STEP_ID" in report.error_codes()

    def test_unknown_dependency_endpoint_rejected(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S9"},
        ]))
        assert "UNKNOWN_STEP_REF" in report.error_codes()

    def test_self_loop_rejected(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S1"},
        ]))
        assert "SELF_LOOP" in report.error_codes()

    def test_cycle_rejected_with_path(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S2"},
            {"from_step_id": "S2", "to_step_id": "S1"},
        ]))
        assert "CYCLE" in report.error_codes()
        message = next(i.message for i in report.issues if i.code == "CYCLE")
        assert "S1" in message and "S2" in message and "→" in message

    def test_data_dependency_requires_binding(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S2", "kind": "data"},
        ]))
        assert "MISSING_DATA_BINDING" in report.error_codes()

    def test_unknown_output_binding_rejected(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S2", "kind": "data",
             "binding": {"output_name": "nope", "input_name": "ev"}},
        ]))
        assert "UNKNOWN_OUTPUT_BINDING" in report.error_codes()

    def test_unknown_input_binding_rejected(self):
        report = validate_plan(_plan(dependencies=[
            {"from_step_id": "S1", "to_step_id": "S2", "kind": "data",
             "binding": {"output_name": "evidence", "input_name": "nope"}},
        ]))
        assert "UNKNOWN_INPUT_BINDING" in report.error_codes()

    def test_schema_type_mismatch_rejected(self):
        payload = _plan()
        payload["steps"][1]["inputs"][0]["expected_schema"] = {"type": "string"}
        report = validate_plan(payload)
        assert "SCHEMA_TYPE_MISMATCH" in report.error_codes()

    def test_tool_outside_allowlist_rejected(self):
        report = validate_plan(_plan(), allowed_tools={"other_tool"})
        assert "UNKNOWN_TOOL" in report.error_codes()

    def test_tool_step_without_tool_name_rejected(self):
        payload = _plan()
        payload["steps"][0].pop("tool_name")
        report = validate_plan(payload)
        assert "MISSING_TOOL_NAME" in report.error_codes()

    def test_budget_infeasible_when_reserve_not_left(self):
        report = validate_plan(_plan(), remaining_tokens=1000, recovery_reserve=0.1,
                               )
        # 计划没有声明估算 → 不拦；声明了才拦
        assert "BUDGET_INFEASIBLE" not in report.error_codes()

        payload = _plan(estimated_budget={"max_model_tokens": 5000})
        bad = validate_plan(payload, remaining_tokens=1000, recovery_reserve=0.1)
        assert "BUDGET_INFEASIBLE" in bad.error_codes()

    def test_budget_warns_when_remaining_unknown(self):
        report = validate_plan(_plan(estimated_budget={"max_model_tokens": 500}))
        assert "BUDGET_UNVERIFIED" in [item.code for item in report.warnings]


class TestHumanGates:
    def test_step_without_executable_criteria_needs_human(self):
        payload = _plan()
        payload["steps"][2].pop("success_criteria")
        report = validate_plan(payload)
        assert report.ok, "计划本身可激活，但要人工闸门"
        assert "S3" in report.human_required
        assert "NO_EXECUTABLE_CRITERIA" in [item.code for item in report.warnings]

    def test_model_judge_alone_is_not_executable(self):
        payload = _plan()
        payload["steps"][2]["success_criteria"] = [{
            "criterion_id": "c3", "kind": "test_result",
            "evaluator": "model_judge", "config": {"field": "ok"},
        }]
        report = validate_plan(payload)
        assert "S3" in report.human_required

    def test_unknown_precondition_without_probe_needs_human(self):
        payload = _plan()
        payload["steps"] = [{"step_id": "S1", "title": "只有一个 llm 步",
                             "kind": "llm",
                             "preconditions": [{"check": "file_exists", "truth": "unknown"}],
                             "success_criteria": [{"criterion_id": "c1",
                                                    "kind": "json_schema",
                                                    "config": {"required": ["x"]}}]}]
        payload["dependencies"] = []
        report = validate_plan(payload)
        assert "S1" in report.human_required
        assert "UNKNOWN_PRECONDITION" in [item.code for item in report.warnings]

    def test_optional_step_without_criteria_is_only_a_warning(self):
        payload = _plan()
        payload["steps"][2]["optional"] = True
        payload["steps"][2].pop("success_criteria")
        report = validate_plan(payload)
        assert report.ok
        assert "S3" not in report.human_required
        assert "OPTIONAL_STEP_WITHOUT_CRITERIA" in [item.code for item in report.warnings]


def test_step_status_includes_indeterminate():
    """§5.2 的转移表用到 indeterminate，§4.4 的枚举列表漏了它；实现按转移表补齐。"""
    assert StepStatus("indeterminate") is StepStatus.INDETERMINATE
