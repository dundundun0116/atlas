"""Plan Validator：计划先要**可执行**，才允许被激活（设计文档 §5.4）。

七道闸，按文档顺序：
1. 结构/schema 校验（解析失败直接拒绝）；
2. ID 唯一 + 引用完整；
3. DAG 无环 + `data` 边必须有 binding，且两端能对上具体输出/输入与 schema 类型；
4. 自动执行的 Step 必须有**可执行**判据（纯模型自评不算），否则转人工；
5. `tool_name` 在 allowlist 内；
6. 前置条件只能是 true/false/unknown；unknown 未被探测 Step 覆盖 → 转人工；
7. 预算总和 + 恢复预留可行。

结论分三档，不混为一谈：
- `issues`（errors）：**拒绝激活**；
- `human_required`：计划可以激活，但这些 Step 必须走人工闸门（`needs_human`），
  不允许模型自己宣布成功；
- `warnings`：可解释的提醒（例如 optional Step 被跳过）。

校验器是纯函数：给定同一份计划与同一组外部事实，结论可复现。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from atlas.planning.schemas import (
    DependencyKind,
    PlanSchemaError,
    PlanSpec,
    StepKind,
    StepSpec,
    VALIDATOR_VERSION,
)

#: 兜底比例：预算里必须留出恢复用的一部分，不允许把全部预算排满（§4.11）。
DEFAULT_RECOVERY_RESERVE = 0.1

#: 需要人工闸门的 Step 种类。
_HUMAN_KINDS = (StepKind.HUMAN_GATE,)

#: 需要 tool_name 的 Step 种类。
_TOOL_KINDS = (StepKind.TOOL,)


@dataclass
class PlanIssue:
    code: str
    message: str
    step_id: str = ""
    dependency: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "step_id": self.step_id,
            "dependency": self.dependency,
        }


@dataclass
class PlanValidationReport:
    """一次校验的完整结论：能不能激活、哪些步要人、执行顺序是什么。"""

    ok: bool = False
    issues: List[PlanIssue] = field(default_factory=list)
    warnings: List[PlanIssue] = field(default_factory=list)
    human_required: List[str] = field(default_factory=list)
    topological_order: List[str] = field(default_factory=list)
    validator_version: str = VALIDATOR_VERSION

    @property
    def activatable(self) -> bool:
        return self.ok and not self.issues

    def error_codes(self) -> List[str]:
        return [issue.code for issue in self.issues]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "activatable": self.activatable,
            "issues": [item.to_dict() for item in self.issues],
            "warnings": [item.to_dict() for item in self.warnings],
            "human_required": list(self.human_required),
            "topological_order": list(self.topological_order),
            "validator_version": self.validator_version,
        }


def _adjacency(plan: PlanSpec) -> Dict[str, List[str]]:
    graph: Dict[str, List[str]] = {step.step_id: [] for step in plan.steps}
    for dependency in plan.dependencies:
        graph.setdefault(dependency.from_step_id, []).append(dependency.to_step_id)
    return graph


def _kahn_order(plan: PlanSpec) -> Tuple[List[str], Set[str]]:
    """拓扑序 + 环上的节点；环上节点集合用于给出可定位的报错。"""
    graph = _adjacency(plan)
    indegree = {node: 0 for node in graph}
    for targets in graph.values():
        for target in targets:
            if target in indegree:
                indegree[target] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    order: List[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for target in graph.get(node, []):
            if target not in indegree:
                continue
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
        queue.sort()
    cyclic = {node for node, degree in indegree.items() if degree > 0}
    return order, cyclic


def _first_cycle(plan: PlanSpec, cyclic: Set[str]) -> List[str]:
    """在环上找一个具体路径，报错里带路径才可定位。"""
    graph = _adjacency(plan)

    def walk(node: str, path: List[str], seen: Set[str]) -> Optional[List[str]]:
        for target in graph.get(node, []):
            if target not in cyclic:
                continue
            if target in path:
                return path[path.index(target):] + [target]
            found = walk(target, path + [target], seen | {target})
            if found:
                return found
        return None

    for node in sorted(cyclic):
        found = walk(node, [node], {node})
        if found:
            return found
    return sorted(cyclic)


def _schema_types_compatible(upstream: Dict[str, Any], downstream: Dict[str, Any]) -> bool:
    """只比 `type`：两边都声明且不同才判不兼容（不假装做了完整 JSON Schema 校验）。"""
    up_type = (upstream or {}).get("type")
    down_type = (downstream or {}).get("type")
    if up_type and down_type and up_type != down_type:
        return False
    return True


def _is_actionable(step: StepSpec) -> bool:
    """至少一条 required 且非纯模型自评的判据，才算"可以被自动判定完成"。"""
    return any(item.required and item.is_actionable for item in step.success_criteria)


def _has_probe_coverage(step: StepSpec, plan: PlanSpec) -> bool:
    """unknown 前置条件是否被某个探测型 Step 覆盖（同计划的 verify/tool 且先执行）。"""
    probe_kinds = (StepKind.VERIFY, StepKind.TOOL)
    return any(
        other.step_id != step.step_id and other.kind in probe_kinds
        for other in plan.steps
    )


def _check_structure(plan: PlanSpec, report: PlanValidationReport) -> None:
    seen: Dict[str, int] = {}
    for step in plan.steps:
        seen[step.step_id] = seen.get(step.step_id, 0) + 1
    for step_id, count in sorted(seen.items()):
        if count > 1:
            report.issues.append(PlanIssue(
                code="DUPLICATE_STEP_ID",
                message=f"step_id {step_id!r} 出现 {count} 次；计划内 id 必须唯一",
                step_id=step_id,
            ))
    if not plan.steps:
        report.issues.append(PlanIssue(code="EMPTY_PLAN", message="计划没有 Step"))


def _check_references(plan: PlanSpec, report: PlanValidationReport) -> None:
    known = {step.step_id for step in plan.steps}
    for dependency in plan.dependencies:
        label = f"{dependency.from_step_id}->{dependency.to_step_id}"
        unknown = [
            side for side in (dependency.from_step_id, dependency.to_step_id)
            if side not in known
        ]
        if unknown:
            report.issues.append(PlanIssue(
                code="UNKNOWN_STEP_REF",
                message=f"依赖引用了不存在的 Step：{unknown}",
                dependency=label,
            ))
        if dependency.from_step_id == dependency.to_step_id:
            report.issues.append(PlanIssue(
                code="SELF_LOOP",
                message=f"自环依赖：{dependency.from_step_id} 依赖自己",
                step_id=dependency.from_step_id,
                dependency=label,
            ))


def _check_dag(plan: PlanSpec, report: PlanValidationReport) -> None:
    order, cyclic = _kahn_order(plan)
    report.topological_order = order
    if cyclic:
        path = _first_cycle(plan, cyclic)
        report.issues.append(PlanIssue(
            code="CYCLE",
            message=f"依赖成环：{' → '.join(path)}",
        ))


def _check_bindings(plan: PlanSpec, report: PlanValidationReport) -> None:
    for dependency in plan.dependencies:
        if dependency.kind is not DependencyKind.DATA:
            continue
        label = f"{dependency.from_step_id}->{dependency.to_step_id}"
        if not dependency.binding:
            report.issues.append(PlanIssue(
                code="MISSING_DATA_BINDING",
                message="data 依赖必须声明 binding（output_name → input_name）",
                dependency=label,
            ))
            continue
        upstream = plan.step(dependency.from_step_id)
        downstream = plan.step(dependency.to_step_id)
        if upstream is None or downstream is None:
            continue  # 已在引用检查里报过
        output_name = dependency.binding.get("output_name", "")
        input_name = dependency.binding.get("input_name", "")
        output = upstream.output_named(output_name)
        binding_in = downstream.input_named(input_name)
        if output is None:
            report.issues.append(PlanIssue(
                code="UNKNOWN_OUTPUT_BINDING",
                message=f"上游 {upstream.step_id} 没有名为 {output_name!r} 的 outputs",
                step_id=upstream.step_id,
                dependency=label,
            ))
        if binding_in is None:
            report.issues.append(PlanIssue(
                code="UNKNOWN_INPUT_BINDING",
                message=f"下游 {downstream.step_id} 没有名为 {input_name!r} 的 inputs",
                step_id=downstream.step_id,
                dependency=label,
            ))
        if output is not None and binding_in is not None:
            if not _schema_types_compatible(output.schema, binding_in.expected_schema):
                report.issues.append(PlanIssue(
                    code="SCHEMA_TYPE_MISMATCH",
                    message=(
                        f"binding 类型不兼容：{upstream.step_id}.{output_name} "
                        f"({output.schema.get('type')}) → {downstream.step_id}.{input_name} "
                        f"({binding_in.expected_schema.get('type')})"
                    ),
                    dependency=label,
                ))


def _check_criteria(plan: PlanSpec, report: PlanValidationReport) -> None:
    """自动执行的 Step 必须有可执行判据；否则不能被模型自己宣布完成。"""
    for step in plan.steps:
        if step.kind in _HUMAN_KINDS:
            continue
        if _is_actionable(step):
            continue
        if step.optional:
            report.warnings.append(PlanIssue(
                code="OPTIONAL_STEP_WITHOUT_CRITERIA",
                message=f"可选 Step {step.step_id} 没有可执行判据，只能被跳过",
                step_id=step.step_id,
            ))
            continue
        report.human_required.append(step.step_id)
        report.warnings.append(PlanIssue(
            code="NO_EXECUTABLE_CRITERIA",
            message=(
                f"Step {step.step_id} 没有可执行的 success_criteria"
                f"（{len(step.success_criteria)} 条，纯 model_judge 不算）→ 转人工闸门"
            ),
            step_id=step.step_id,
        ))


def _check_tools(plan: PlanSpec, report: PlanValidationReport,
                 allowed_tools: Optional[Sequence[str]]) -> None:
    allowlist = set(allowed_tools) if allowed_tools is not None else None
    for step in plan.steps:
        if step.kind in _TOOL_KINDS and not step.tool_name:
            report.issues.append(PlanIssue(
                code="MISSING_TOOL_NAME",
                message=f"tool Step {step.step_id} 必须声明 tool_name",
                step_id=step.step_id,
            ))
        if allowlist is not None and step.tool_name and step.tool_name not in allowlist:
            report.issues.append(PlanIssue(
                code="UNKNOWN_TOOL",
                message=f"tool {step.tool_name!r} 不在 allowlist {sorted(allowlist)}",
                step_id=step.step_id,
            ))


def _check_preconditions(plan: PlanSpec, report: PlanValidationReport) -> None:
    for step in plan.steps:
        unknown = [
            item for item in step.preconditions
            if str(item.get("truth", "")).lower() == "unknown"
        ]
        if unknown and not _has_probe_coverage(step, plan):
            report.human_required.append(step.step_id)
            report.warnings.append(PlanIssue(
                code="UNKNOWN_PRECONDITION",
                message=(
                    f"Step {step.step_id} 有 {len(unknown)} 条前置条件真值未知，"
                    "且计划里没有可覆盖它的探测 Step → 转人工"
                ),
                step_id=step.step_id,
            ))


def _step_estimate(step: StepSpec) -> float:
    for key in ("estimated_model_tokens", "max_model_tokens"):
        value = step.budget.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _check_budget(plan: PlanSpec, report: PlanValidationReport,
                  remaining_tokens: Optional[float],
                  recovery_reserve: float) -> None:
    """预算总要留恢复预留；排满就没有重试和重规划的空间（§4.11）。"""
    declared = sum(_step_estimate(step) for step in plan.steps)
    plan_estimate = plan.estimated_budget.get("max_model_tokens")
    total = float(plan_estimate) if isinstance(plan_estimate, (int, float)) else declared
    if remaining_tokens is None:
        if total:
            report.warnings.append(PlanIssue(
                code="BUDGET_UNVERIFIED",
                message=f"计划预估 {total:.0f} tokens，但未提供剩余预算：无法确认可行性",
            ))
        return
    reserve = float(remaining_tokens) * float(recovery_reserve)
    if total > float(remaining_tokens) - reserve:
        report.issues.append(PlanIssue(
            code="BUDGET_INFEASIBLE",
            message=(
                f"计划预估 {total:.0f} tokens，超过可用预算 "
                f"{float(remaining_tokens) - reserve:.0f}（已扣 {recovery_reserve:.0%} 恢复预留）"
            ),
        ))


def validate_plan(
    plan: PlanSpec | Dict[str, Any],
    *,
    allowed_tools: Optional[Iterable[str]] = None,
    remaining_tokens: Optional[float] = None,
    recovery_reserve: float = DEFAULT_RECOVERY_RESERVE,
) -> PlanValidationReport:
    """把候选计划过一遍七道闸；返回可留档的报告，不抛业务异常。"""
    report = PlanValidationReport()
    try:
        spec = plan if isinstance(plan, PlanSpec) else PlanSpec.from_dict(plan)
    except PlanSchemaError as exc:
        report.issues.append(PlanIssue(code="SCHEMA_INVALID", message=str(exc)))
        return report

    _check_structure(spec, report)
    _check_references(spec, report)
    _check_dag(spec, report)
    _check_bindings(spec, report)
    _check_criteria(spec, report)
    _check_tools(spec, report, list(allowed_tools) if allowed_tools is not None else None)
    _check_preconditions(spec, report)
    _check_budget(spec, report, remaining_tokens, recovery_reserve)

    report.human_required = sorted(set(report.human_required))
    report.ok = not report.issues
    return report


def validation_problems(report: PlanValidationReport) -> List[str]:
    """给人类看的一行一条，方便 CLI / 报告直接展示。"""
    return [f"{issue.code}: {issue.message}" for issue in report.issues]
