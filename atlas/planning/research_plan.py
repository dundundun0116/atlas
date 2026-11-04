"""把单问题研究流程写成**显式计划**（设计文档 §11 Phase 2 的迁移那一条）。

    plan → retrieve(optional) → draft → verify → repair(optional) → final_verify

与 `legacy_research_v1`（`Orchestrator` 里的固定三段式）的区别不是"换个写法"，而是：

- 每一步**先在库里**（`plans/steps/dependencies`），有 id、有依赖、有 binding、有可执行判据；
- 某一步失败/崩溃只重跑那一步，不用从头再跑一遍；
- 上游输出变化可以把下游打成 `stale`，而不是整条重来；
- 完成判定由 `criteria` 求值，不由模型自报。

`build_research_plan` 是**确定性**的（同样的输入给同样的计划），这样测试与报告都能复现。
"""

from typing import Any, Dict, List

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

#: 这条工作流的版本标记；与 `legacy_research_v1` 明确区分。
RESEARCH_PLAN_WORKFLOW = "plan_dag_v1"

_STEP_ORDER = {"retrieve": 10, "draft": 20, "verify": 30, "repair": 40, "final_verify": 50}


def _criterion(name: str, kind: str, config: Dict[str, Any]) -> Criterion:
    return Criterion(criterion_id=name, kind=kind, config=dict(config))


def build_research_plan(
    problem: str,
    *,
    with_research: bool = True,
    with_repair: bool = True,
    budget_tokens: int = 4000,
) -> PlanSpec:
    """为一个研究问题生成显式计划（DAG，串行执行）。"""
    problem = str(problem or "").strip()
    if not problem:
        raise ValueError("problem 不能为空")

    steps: List[StepSpec] = []
    dependencies: List[DependencySpec] = []

    if with_research:
        steps.append(StepSpec(
            step_id="retrieve",
            title="检索证据",
            objective=f"为问题取回可引用证据：{problem}",
            kind=StepKind.TOOL,
            tool_name="compassrag",
            ordinal=_STEP_ORDER["retrieve"],
            optional=True,
            budget={"max_model_tokens": budget_tokens},
            outputs=[OutputSpec(name="refs", schema={"type": "array"}),
                     OutputSpec(name="answer", schema={"type": "string"})],
            success_criteria=[_criterion("evidence", "evidence_support",
                                         {"min_refs": 1, "field": "refs"})],
        ))

    steps.append(StepSpec(
        step_id="draft",
        title="起草答案",
        objective="按证据与问题写出草稿",
        kind=StepKind.LLM,
        ordinal=_STEP_ORDER["draft"],
        inputs=[InputBinding(name="evidence", source_kind="step_output",
                             required=False, expected_schema={"type": "array"})],
        outputs=[OutputSpec(name="draft", schema={"type": "string"})],
        success_criteria=[_criterion("draft_present", "json_schema",
                                     {"required": ["draft"]})],
    ))

    steps.append(StepSpec(
        step_id="verify",
        title="核验草稿",
        objective="按有效约束与证据核验草稿，给出 verdict 与问题清单",
        kind=StepKind.LLM,
        ordinal=_STEP_ORDER["verify"],
        inputs=[InputBinding(name="draft", source_kind="step_output",
                             required=True, expected_schema={"type": "string"})],
        outputs=[OutputSpec(name="verdict", schema={"type": "string"}),
                 OutputSpec(name="issues", schema={"type": "string"})],
        success_criteria=[_criterion("verdict_present", "json_schema",
                                     {"required": ["verdict", "issues"]})],
    ))

    if with_repair:
        steps.append(StepSpec(
            step_id="repair",
            title="按核验意见修订",
            objective="仅当核验判需修订时改稿；否则原样返回草稿",
            kind=StepKind.LLM,
            ordinal=_STEP_ORDER["repair"],
            optional=True,
            inputs=[InputBinding(name="draft", source_kind="step_output",
                                 required=True, expected_schema={"type": "string"}),
                    InputBinding(name="issues", source_kind="step_output",
                                 required=False, expected_schema={"type": "string"})],
            outputs=[OutputSpec(name="draft", schema={"type": "string"})],
            success_criteria=[_criterion("revised_present", "json_schema",
                                         {"required": ["draft"]})],
        ))

    steps.append(StepSpec(
        step_id="final_verify",
        title="终检",
        objective="对最终稿给出最终 verdict；没有可执行判据就不算完成",
        kind=StepKind.VERIFY,
        ordinal=_STEP_ORDER["final_verify"],
        inputs=[InputBinding(name="draft", source_kind="step_output",
                             required=True, expected_schema={"type": "string"})],
        outputs=[OutputSpec(name="verdict", schema={"type": "string"})],
        # 终检用可执行的布尔判据：verdict 不是 pass 就是失败，
        # 而不是"只要模型吐了个 verdict 字段就算完成"。
        success_criteria=[_criterion("approved", "test_result",
                                     {"field": "approved"})],
    ))

    def _link(src: str, dst: str, *, kind: DependencyKind = DependencyKind.ORDER,
              output_name: str = "", input_name: str = "") -> None:
        binding = ({"output_name": output_name, "input_name": input_name}
                   if output_name and input_name else None)
        dependencies.append(DependencySpec(
            from_step_id=src, to_step_id=dst, kind=kind, binding=binding,
        ))

    if with_research:
        _link("retrieve", "draft", kind=DependencyKind.DATA,
              output_name="refs", input_name="evidence")
    _link("draft", "verify", kind=DependencyKind.DATA,
          output_name="draft", input_name="draft")
    if with_repair:
        _link("draft", "repair", kind=DependencyKind.DATA,
              output_name="draft", input_name="draft")
        _link("verify", "repair", kind=DependencyKind.DATA,
              output_name="issues", input_name="issues")
        _link("repair", "final_verify", kind=DependencyKind.DATA,
              output_name="draft", input_name="draft")
    else:
        _link("draft", "final_verify", kind=DependencyKind.DATA,
              output_name="draft", input_name="draft")

    return PlanSpec(
        steps=steps,
        dependencies=dependencies,
        summary=f"研究流程（{RESEARCH_PLAN_WORKFLOW}）：{problem[:80]}",
        strategy="dag",
        reason="Phase 2：把固定三段式迁移成可持久化、可局部恢复的显式计划",
        generated_by=f"atlas/{RESEARCH_PLAN_WORKFLOW}",
        estimated_budget={"max_model_tokens": budget_tokens * (len(steps) + 1)},
    )


def allowed_tools(*, with_research: bool = True) -> List[str]:
    """这条工作流用到的工具白名单（validator 用）。"""
    return ["compassrag"] if with_research else []
