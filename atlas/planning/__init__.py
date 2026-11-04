"""计划作为一等状态：Plan / Step / Dependency 的数据模型与可执行性校验。

设计文档（`docs/superpowers/plans/2026-11-04-atlas-long-horizon-system-design.md`）
§4.3–4.6 与 §5.4 的落地：计划是数据，不是提示词；先过 validator 的七道闸才允许激活。

`atlas.planning.runner`（PlanRunner）**不在这里导出**：它要用
`atlas.orchestration.scheduler`，而 scheduler 又依赖本包的 schemas——
放进 `__init__` 会形成循环导入。用的时候显式 `from atlas.planning.runner import PlanRunner`。
"""

from atlas.planning.schemas import (
    PLAN_SCHEMA_VERSION,
    VALIDATOR_VERSION,
    Criterion,
    CriterionKind,
    DependencyKind,
    DependencySpec,
    Evaluator,
    InputBinding,
    OutputSpec,
    PlanSchemaError,
    PlanSpec,
    PlanStatus,
    StepKind,
    StepSpec,
    StepStatus,
    steps_in_ordinal_order,
)
from atlas.planning.criteria import (
    evaluate_criterion,
    evaluate_step,
    has_executable_criterion,
)
from atlas.planning.validator import (
    PlanIssue,
    PlanValidationReport,
    validate_plan,
    validation_problems,
)

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "VALIDATOR_VERSION",
    "Criterion",
    "CriterionKind",
    "DependencyKind",
    "DependencySpec",
    "Evaluator",
    "InputBinding",
    "OutputSpec",
    "PlanIssue",
    "PlanValidationReport",
    "PlanSchemaError",
    "PlanSpec",
    "PlanStatus",
    "PlanValidationReport",
    "StepKind",
    "StepSpec",
    "StepStatus",
    "steps_in_ordinal_order",
    "evaluate_criterion",
    "evaluate_step",
    "has_executable_criterion",
    "validate_plan",
    "validation_problems",
]
