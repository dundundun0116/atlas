"""写操作工作流：把"外部写入"也做成显式计划（设计文档 §5.5 / §11 Phase 3 收尾）。

    prepare（可选） → write → confirm

和检索工作流的区别：检索失败最多丢一次查询，写入失败会留下**改了一半的世界**。
所以这条链上每一步都要问"外部到底发生了什么"：

- `write` 步经 `ToolDispatcher` 派发，落 `ToolCall`（幂等键 = 工具 + 作用域 + 参数指纹）；
- 写入**部分生效**时，执行器当场走补偿协议（`run_compensation`），并在 output 里
  如实写 `compensated` / `compensation_failed`——世界干不干净是判据要看的，不是自报的；
- `confirm` 步**读回**目标对象，用布尔判据确认，而不是相信 `write` 的返回值。

`prepare` 留作可选：真实场景里"要写什么"通常由模型决定；测试里可以直接把它省掉，
用 literal 输入喂 `write`。
"""

from typing import Any, Dict

from atlas.planning.schemas import (
    Criterion,
    DependencyKind,
    DependencySpec,
    InputBinding,
    OutputSpec,
    PlanSpec,
    StepKind,
    StepSpec,
)

WRITE_PLAN_WORKFLOW = "write_dag_v1"

WRITE_STEP_ID = "write"
CONFIRM_STEP_ID = "confirm"


def build_write_plan(
    *,
    target: str,
    value: Dict[str, Any],
    tool_name: str = "demo_writer",
    with_prepare: bool = False,
) -> PlanSpec:
    """生成"写入 → 读回确认"的两步计划（可选前置 prepare）。"""
    target = str(target or "").strip()
    if not target:
        raise ValueError("target 不能为空")
    if not isinstance(value, dict) or not value:
        raise ValueError("value 必须是非空对象")

    steps = []
    dependencies = []

    if with_prepare:
        steps.append(StepSpec(
            step_id="prepare",
            title="准备写入内容",
            objective=f"决定写入 {target} 的字段",
            kind=StepKind.LLM,
            ordinal=10,
            outputs=[OutputSpec(name="value", schema={"type": "object"})],
            success_criteria=[Criterion("value_present", "json_schema",
                                        {"required": ["value"]})],
        ))

    steps.append(StepSpec(
        step_id=WRITE_STEP_ID,
        title=f"写入 {target}",
        objective=f"把字段写入外部系统 {target}",
        kind=StepKind.TOOL,
        tool_name=tool_name,
        ordinal=20,
        inputs=[InputBinding(name="target", source_kind="literal")],
        outputs=[OutputSpec(name="target", schema={"type": "string"}),
                 OutputSpec(name="applied", schema={"type": "object"}),
                 OutputSpec(name="compensated", schema={"type": "boolean"})],
        # 判据看的是"写进去了"这个事实；部分生效 / 补偿成功都会让它不过
        success_criteria=[Criterion("write_applied", "test_result",
                                    {"field": "applied_ok"})],
        optional=False,
    ))

    steps.append(StepSpec(
        step_id=CONFIRM_STEP_ID,
        title="读回确认",
        objective="读回目标对象，确认外部世界与预期一致",
        kind=StepKind.VERIFY,
        tool_name=tool_name,
        ordinal=30,
        inputs=[InputBinding(name="target", source_kind="step_output",
                             expected_schema={"type": "string"})],
        outputs=[OutputSpec(name="confirmed", schema={"type": "boolean"})],
        success_criteria=[Criterion("readback_matches", "test_result",
                                    {"field": "confirmed"})],
    ))

    if with_prepare:
        dependencies.append(DependencySpec(
            from_step_id="prepare", to_step_id=WRITE_STEP_ID,
            kind=DependencyKind.DATA,
            binding={"output_name": "value", "input_name": "value"},
        ))
    dependencies.append(DependencySpec(
        from_step_id=WRITE_STEP_ID, to_step_id=CONFIRM_STEP_ID,
        kind=DependencyKind.DATA,
        binding={"output_name": "target", "input_name": "target"},
    ))

    return PlanSpec(
        steps=steps,
        dependencies=dependencies,
        summary=f"写操作（{WRITE_PLAN_WORKFLOW}）：{target}",
        strategy="dag",
        reason="写操作必须能读回确认，并在部分生效时补偿",
        generated_by=f"atlas/{WRITE_PLAN_WORKFLOW}",
        estimated_budget={"max_model_tokens": 2000},
        # 写入参数直接钉在 spec 里：同一计划重跑时幂等键才不会变
        parent_plan_id="",
    )


def allowed_tools(tool_name: str = "demo_writer") -> list:
    return [tool_name]
