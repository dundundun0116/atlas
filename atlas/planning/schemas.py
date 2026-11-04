"""Plan / Step / Dependency：把"计划"当成一等数据（设计文档 §4.3–4.6）。

一句话：计划既不是提示词，也不是框架图，而是**可持久化、可版本化、可验证、可局部替换**
的数据。执行依赖（`Dependency`）与知识图的语义边严格分开——它们不复用 `EdgeRow`，
因为"这一步依赖那一步的输出"和"这个事实支持那个结论"是两种东西。

本模块只放**纯数据 + 结构解析**：
- 校验（环、引用、typed binding、可执行判据）在 `atlas.planning.validator`；
- 调度（谁可以跑）在 `atlas.orchestration.scheduler`；
- 落库（事务、条件 claim）在 `atlas.storage.models` / `repository`。

结构不成立（缺字段、类型不对）抛 `PlanSchemaError`；语义不成立由 validator 出报告，
不在这里抛——因为"计划被拒绝"本身是需要留档的业务结果。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

#: 计划 JSON 的 schema 版本；字段语义变了就 +1，旧计划要能按旧版本读回。
PLAN_SCHEMA_VERSION = 1

#: 校验器版本进 Plan 行：同一份计划换校验器后结论可能不同，必须能追溯。
VALIDATOR_VERSION = "plan-validator/1"


class PlanSchemaError(ValueError):
    """计划候选在结构上就不成立（缺字段 / 类型不对 / 枚举越界）。"""


class PlanStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class StepKind(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    VERIFY = "verify"
    HUMAN_GATE = "human_gate"
    AGGREGATE = "aggregate"


class StepStatus(str, Enum):
    """Step 的十二个状态 + `indeterminate`。

    设计文档 §4.4 的枚举列表列了十二个，而 §5.2 的转移表里出现了
    `dispatched -> indeterminate` / `running -> indeterminate`。两者不一致，
    这里按 §5.2 的表补齐第十三个状态——否则"动作已发出但结果未知"无处安放，
    而它恰恰是长程执行里最常见、也最不能丢的状态。
    """

    BLOCKED = "blocked"
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    NEEDS_HUMAN = "needs_human"
    STALE = "stale"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class DependencyKind(str, Enum):
    ORDER = "order"
    DATA = "data"
    VERIFY = "verify"


class CriterionKind(str, Enum):
    JSON_SCHEMA = "json_schema"
    EXIT_CODE = "exit_code"
    FILE_EXISTS = "file_exists"
    FILE_HASH = "file_hash"
    TEST_RESULT = "test_result"
    EVIDENCE_SUPPORT = "evidence_support"
    CONSTRAINT_CHECK = "constraint_check"
    ENVIRONMENT_PREDICATE = "environment_predicate"
    HUMAN_APPROVAL = "human_approval"


class Evaluator(str, Enum):
    DETERMINISTIC = "deterministic"
    TOOL = "tool"
    MODEL_JUDGE = "model_judge"
    HUMAN = "human"


#: 输入绑定的来源类型（§4.5）。
INPUT_SOURCE_KINDS = (
    "goal",
    "step_output",
    "knowledge_node",
    "environment_observation",
    "literal",
)


def _require(payload: Dict[str, Any], key: str, where: str) -> Any:
    if key not in payload:
        raise PlanSchemaError(f"{where} 缺少必填字段 {key!r}")
    return payload[key]


def _text(payload: Dict[str, Any], key: str, where: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise PlanSchemaError(f"{where} 字段 {key!r} 必须是字符串，收到 {type(value).__name__}")
    return value


def _enum(value: Any, enum_cls, where: str, field_name: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = [item.value for item in enum_cls]
        raise PlanSchemaError(f"{where} 字段 {field_name!r}={value!r} 不在 {allowed}") from exc


def _dict_list(payload: Dict[str, Any], key: str, where: str) -> List[Dict[str, Any]]:
    value = payload.get(key) or []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PlanSchemaError(f"{where} 字段 {key!r} 必须是对象数组")
    return list(value)


@dataclass
class Criterion:
    """一步"算完成"的可执行判据；自动执行的 Step 必须至少有一条。"""

    criterion_id: str
    kind: CriterionKind
    config: Dict[str, Any] = field(default_factory=dict)
    evaluator: Evaluator = Evaluator.DETERMINISTIC
    required: bool = True

    def __post_init__(self) -> None:
        # 允许用手写字符串构造（测试与手写计划常见）；非法值当场报出来，不拖到求值阶段。
        self.kind = CriterionKind(self.kind)
        self.evaluator = Evaluator(self.evaluator)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "criterion") -> "Criterion":
        if not isinstance(payload, dict):
            raise PlanSchemaError(f"{where} 必须是对象")
        criterion_id = _require(payload, "criterion_id", where)
        if not str(criterion_id).strip():
            raise PlanSchemaError(f"{where} criterion_id 不能为空")
        kind = _enum(_require(payload, "kind", where), CriterionKind, where, "kind")
        evaluator = _enum(payload.get("evaluator", "deterministic"),
                          Evaluator, where, "evaluator")
        config = payload.get("config") or {}
        if not isinstance(config, dict):
            raise PlanSchemaError(f"{where} config 必须是对象")
        return cls(
            criterion_id=str(criterion_id),
            kind=kind,
            config=dict(config),
            evaluator=evaluator,
            required=bool(payload.get("required", True)),
        )

    @property
    def is_actionable(self) -> bool:
        """模型自评（model_judge）单独一条不算可执行判据（§4.5）。"""
        return self.evaluator is not Evaluator.MODEL_JUDGE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "kind": self.kind.value,
            "config": dict(self.config),
            "evaluator": self.evaluator.value,
            "required": self.required,
        }


@dataclass
class OutputSpec:
    """一步产出什么；下游 `data` 依赖的 binding 必须能在这里找到。"""

    name: str
    schema: Dict[str, Any] = field(default_factory=dict)
    artifact_kind: str = "text"
    durable: bool = True
    provenance_required: bool = False

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "output") -> "OutputSpec":
        name = str(_require(payload, "name", where)).strip()
        if not name:
            raise PlanSchemaError(f"{where} name 不能为空")
        schema = payload.get("schema") or {}
        if not isinstance(schema, dict):
            raise PlanSchemaError(f"{where} schema 必须是对象")
        return cls(
            name=name,
            schema=dict(schema),
            artifact_kind=str(payload.get("artifact_kind") or "text"),
            durable=bool(payload.get("durable", True)),
            provenance_required=bool(payload.get("provenance_required", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "schema": dict(self.schema),
            "artifact_kind": self.artifact_kind,
            "durable": self.durable,
            "provenance_required": self.provenance_required,
        }


@dataclass
class InputBinding:
    """一步消费什么；`step_output` 必须由一条 `data` 依赖绑定到上游输出。"""

    name: str
    source_kind: str = "literal"
    source_id: str = ""
    source_path: str = ""
    required: bool = True
    expected_schema: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "input") -> "InputBinding":
        name = str(_require(payload, "name", where)).strip()
        if not name:
            raise PlanSchemaError(f"{where} name 不能为空")
        source_kind = str(payload.get("source_kind") or "literal")
        if source_kind not in INPUT_SOURCE_KINDS:
            raise PlanSchemaError(f"{where} source_kind={source_kind!r} 不在 {list(INPUT_SOURCE_KINDS)}")
        expected = payload.get("expected_schema") or {}
        if not isinstance(expected, dict):
            raise PlanSchemaError(f"{where} expected_schema 必须是对象")
        return cls(
            name=name,
            source_kind=source_kind,
            source_id=str(payload.get("source_id") or ""),
            source_path=str(payload.get("source_path") or ""),
            required=bool(payload.get("required", True)),
            expected_schema=dict(expected),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_path": self.source_path,
            "required": self.required,
            "expected_schema": dict(self.expected_schema),
        }


@dataclass
class StepSpec:
    """最小可调度与恢复单元（§4.4）。状态不属于 spec，属于持久化行。"""

    step_id: str
    title: str
    objective: str = ""
    kind: StepKind = StepKind.LLM
    instructions: str = ""
    inputs: List[InputBinding] = field(default_factory=list)
    outputs: List[OutputSpec] = field(default_factory=list)
    preconditions: List[Dict[str, Any]] = field(default_factory=list)
    success_criteria: List[Criterion] = field(default_factory=list)
    tool_name: str = ""
    tool_args_template: Dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 1
    ordinal: int = 0
    optional: bool = False
    budget: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "step") -> "StepSpec":
        step_id = str(_require(payload, "step_id", where)).strip()
        if not step_id:
            raise PlanSchemaError(f"{where} step_id 不能为空")
        where = f"step[{step_id}]"
        title = _text(payload, "title", where) or step_id
        kind = _enum(payload.get("kind", "llm"), StepKind, where, "kind")
        max_attempts = payload.get("max_attempts", 1)
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise PlanSchemaError(f"{where} max_attempts 必须是 >=1 的整数")
        ordinal = payload.get("ordinal", 0)
        if not isinstance(ordinal, int):
            raise PlanSchemaError(f"{where} ordinal 必须是整数")
        return cls(
            step_id=step_id,
            title=title,
            objective=_text(payload, "objective", where),
            kind=kind,
            instructions=_text(payload, "instructions", where),
            inputs=[InputBinding.from_dict(item, f"{where}.input")
                    for item in _dict_list(payload, "inputs", where)],
            outputs=[OutputSpec.from_dict(item, f"{where}.output")
                     for item in _dict_list(payload, "outputs", where)],
            preconditions=_dict_list(payload, "preconditions", where),
            success_criteria=[Criterion.from_dict(item, f"{where}.criterion")
                              for item in _dict_list(payload, "success_criteria", where)],
            tool_name=_text(payload, "tool_name", where),
            tool_args_template=dict(payload.get("tool_args_template") or {}),
            max_attempts=max_attempts,
            ordinal=ordinal,
            optional=bool(payload.get("optional", False)),
            budget=dict(payload.get("budget") or {}),
        )

    def output_named(self, name: str) -> Optional[OutputSpec]:
        return next((item for item in self.outputs if item.name == name), None)

    def input_named(self, name: str) -> Optional[InputBinding]:
        return next((item for item in self.inputs if item.name == name), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "objective": self.objective,
            "kind": self.kind.value,
            "instructions": self.instructions,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "preconditions": [dict(item) for item in self.preconditions],
            "success_criteria": [item.to_dict() for item in self.success_criteria],
            "tool_name": self.tool_name,
            "tool_args_template": dict(self.tool_args_template),
            "max_attempts": self.max_attempts,
            "ordinal": self.ordinal,
            "optional": self.optional,
            "budget": dict(self.budget),
        }


@dataclass
class DependencySpec:
    """执行依赖；kind=data 时必须有 binding，且两端能对上具体输入/输出。"""

    from_step_id: str
    to_step_id: str
    kind: DependencyKind = DependencyKind.ORDER
    binding: Optional[Dict[str, str]] = None
    condition: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "dependency") -> "DependencySpec":
        from_id = str(_require(payload, "from_step_id", where)).strip()
        to_id = str(_require(payload, "to_step_id", where)).strip()
        if not from_id or not to_id:
            raise PlanSchemaError(f"{where} from_step_id / to_step_id 不能为空")
        kind = _enum(payload.get("kind", "order"), DependencyKind, where, "kind")
        binding = payload.get("binding")
        if binding is not None:
            if not isinstance(binding, dict):
                raise PlanSchemaError(f"{where} binding 必须是对象")
            binding = {
                "output_name": str(binding.get("output_name") or "").strip(),
                "input_name": str(binding.get("input_name") or "").strip(),
            }
        condition = payload.get("condition") or {}
        if not isinstance(condition, dict):
            raise PlanSchemaError(f"{where} condition 必须是对象")
        return cls(
            from_step_id=from_id,
            to_step_id=to_id,
            kind=kind,
            binding=binding,
            condition=dict(condition),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_step_id": self.from_step_id,
            "to_step_id": self.to_step_id,
            "kind": self.kind.value,
            "binding": dict(self.binding) if self.binding else None,
            "condition": dict(self.condition),
        }


@dataclass
class PlanSpec:
    """一份计划候选（一个 revision）；激活与版本由 repository 管。"""

    steps: List[StepSpec]
    dependencies: List[DependencySpec] = field(default_factory=list)
    summary: str = ""
    strategy: str = "dag"
    reason: str = ""
    generated_by: str = "model"
    estimated_budget: Dict[str, Any] = field(default_factory=dict)
    parent_plan_id: str = ""
    schema_version: int = PLAN_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], where: str = "plan") -> "PlanSpec":
        if not isinstance(payload, dict):
            raise PlanSchemaError(f"{where} 必须是对象")
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanSchemaError(f"{where} 必须给出非空的 steps 数组")
        steps = [StepSpec.from_dict(item, f"{where}.steps") for item in raw_steps]
        dependencies = [
            DependencySpec.from_dict(item, f"{where}.dependencies")
            for item in _dict_list(payload, "dependencies", where)
        ]
        version = payload.get("schema_version", PLAN_SCHEMA_VERSION)
        if not isinstance(version, int):
            raise PlanSchemaError(f"{where} schema_version 必须是整数")
        return cls(
            steps=steps,
            dependencies=dependencies,
            summary=_text(payload, "summary", where),
            strategy=_text(payload, "strategy", where, "dag") or "dag",
            reason=_text(payload, "reason", where),
            generated_by=_text(payload, "generated_by", where, "model") or "model",
            estimated_budget=dict(payload.get("estimated_budget") or {}),
            parent_plan_id=str(payload.get("parent_plan_id") or ""),
            schema_version=version,
        )

    @property
    def step_ids(self) -> List[str]:
        return [step.step_id for step in self.steps]

    def step(self, step_id: str) -> Optional[StepSpec]:
        return next((item for item in self.steps if item.step_id == step_id), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "strategy": self.strategy,
            "reason": self.reason,
            "generated_by": self.generated_by,
            "estimated_budget": dict(self.estimated_budget),
            "parent_plan_id": self.parent_plan_id,
            "steps": [item.to_dict() for item in self.steps],
            "dependencies": [item.to_dict() for item in self.dependencies],
        }


def steps_in_ordinal_order(steps: Sequence[StepSpec]) -> List[StepSpec]:
    """稳定展示顺序：ordinal，其次 step_id（不代替依赖，只用于同层排序）。"""
    return sorted(steps, key=lambda item: (item.ordinal, item.step_id))
