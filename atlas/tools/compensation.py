"""补偿协议与人工审批门槛（设计文档 §4.8 / §5.5 / §13）。

`ToolCall` 停在 `failed_partial`（部分生效）时，正确的下一步**既不是重试也不是放弃**：
重试会把已经生效的那一半再做一次，放弃则留下半成品。唯一正确的动作是**补偿**——
再发一个方向相反的新调用，并用 `compensation_for` 指回原调用。

三条纪律：

1. **补偿是一条新调用**，原调用历史只读不改（可审计）；
2. **能不能自动补偿看工具能力**：`supports_compensation=False` 的外部副作用工具，
   只能升级给人；
3. **高风险动作要人工审批**：不可补偿的外部副作用，派发前就该过闸门，
   而不是出事了再问。

这里只做"决定"，不执行；执行走 `atlas.tools.dispatcher`，落库走 repository。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from atlas.tools.calls import EffectClass, ToolCallStatus, ToolSpec

#: 恢复动作；与设计文档 §5.5 的粒度对齐。
ACTIONS = ("none", "retry", "reconcile", "compensate", "human")


@dataclass
class CompensationDecision:
    action: str
    reason: str
    compensation_args: Optional[Dict[str, Any]] = None

    @property
    def needs_human(self) -> bool:
        return self.action == "human"

    @property
    def should_compensate(self) -> bool:
        return self.action == "compensate"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "compensation_args": dict(self.compensation_args or {}) or None,
        }


def decide_after_call(
    call: Dict[str, Any],
    spec: ToolSpec,
    *,
    compensation_args: Optional[Dict[str, Any]] = None,
) -> CompensationDecision:
    """一次工具调用"之后"该怎么办（只看状态与能力，不看业务语义）。"""
    status = str(call.get("status") or "")
    outcome = str(call.get("outcome") or "")

    if status in (ToolCallStatus.SETTLED.value,) and outcome in ("applied", ""):
        return CompensationDecision("none", "调用已结算并生效")
    if status == ToolCallStatus.RECONCILED.value and outcome == "applied":
        return CompensationDecision("none", "对账确认已生效")
    if status == ToolCallStatus.RECONCILED.value and outcome == "partially_applied":
        return _compensate_or_human(spec, compensation_args, "对账确认部分生效")
    if status == ToolCallStatus.FAILED_PARTIAL.value:
        return _compensate_or_human(spec, compensation_args, "调用部分生效")
    if status == ToolCallStatus.INDETERMINATE.value:
        if spec.supports_lookup:
            return CompensationDecision("reconcile", "结果未知：先按幂等键对账")
        return CompensationDecision("human", "结果未知且工具不支持查询，只能人工确认")
    if status == ToolCallStatus.FAILED_DETERMINATE.value:
        if outcome == "not_applied" and spec.retry_safe:
            return CompensationDecision("retry", "确定未生效且可安全重试")
        return CompensationDecision("human", "确定失败但不可安全重试，交人工决定")
    if status == ToolCallStatus.COMPENSATING.value:
        return CompensationDecision("none", "补偿已在进行中")
    return CompensationDecision("human", f"状态 {status or 'unknown'} 没有自动处置策略")


def _compensate_or_human(spec: ToolSpec, args: Optional[Dict[str, Any]],
                         reason: str) -> CompensationDecision:
    if not spec.supports_compensation:
        return CompensationDecision("human", f"{reason}，但工具不支持补偿")
    if not args:
        return CompensationDecision("human", f"{reason}，但没有可执行的补偿参数")
    return CompensationDecision("compensate", reason, dict(args))


def requires_human_approval(spec: ToolSpec) -> bool:
    """派发**之前**的闸门：不可补偿的外部副作用必须人工批准。"""
    if spec.effect_class is not EffectClass.EXTERNAL_SIDE_EFFECT:
        return False
    return not spec.supports_compensation


def compensation_scope(tool_call_id: str) -> str:
    """补偿调用的幂等作用域：一条原调用只对应一个补偿。"""
    return f"compensate:{tool_call_id}"


def run_compensation(
    repository,
    session_id: str,
    call: Dict[str, Any],
    spec: ToolSpec,
    *,
    compensation_args: Dict[str, Any],
    perform,
    actor: str = "compensator",
) -> Dict[str, Any]:
    """按协议执行一次补偿：新调用（`compensation_for` 指回原调用）→ 派发 → 结算。

    `perform(args)` 是真正的反向动作；它抛异常时补偿调用落 `failed_determinate`
    （确定没补偿成功），而不是当成"不知道"。
    """
    from atlas.tools.dispatcher import ToolDispatcher

    original_id = call["tool_call_id"]
    proposal = repository.propose_tool_call(
        session_id, tool_name=spec.name, args=compensation_args,
        step_id=call.get("step_id", ""), plan_id=call.get("plan_id", ""),
        effect_class=spec.effect_class.value, tool_version=spec.version,
        scope=compensation_scope(original_id), actor=actor,
    )
    compensation_id = proposal["tool_call"]["tool_call_id"]
    repository.link_compensation(session_id, compensation_id, original_id)

    dispatcher = ToolDispatcher(repository, session_id, spec,
                                plan_id=call.get("plan_id", ""),
                                step_id=call.get("step_id", ""),
                                scope=compensation_scope(original_id),
                                actor=actor)
    outcome = dispatcher.invoke(lambda: perform(compensation_args), compensation_args)
    return {
        "compensation_id": compensation_id,
        "resolved": proposal["created"],
        "status": outcome.status.value,
        "outcome": outcome.outcome,
        "result": dict(outcome.result or {}),
        "error": dict(outcome.error or {}),
    }
