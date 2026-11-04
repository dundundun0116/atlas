"""工具调用记录与 unknown 对账（设计文档 §4.8 / §6 / §9.1）。

现实是 **at-least-once**：请求超时不等于动作没执行。所以一次外部调用必须先在库里
留下"意图"，再派发，再把结果落成**四档确定结论**之一：

    applied / not_applied / partially_applied / unknowable

`unknowable`（对账也判不出来）不是失败，而是一等状态——它冻结依赖它的下游、
等人工或等对账，**绝不盲目重发**（重发就可能产生重复副作用）。

本模块只管"记录与分类"：状态机、状态转移、幂等键与参数指纹。
真正的派发/重试/补偿编排在 `atlas.tools.dispatcher`。
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional

#: 调用带来的副作用类型；决定"能不能重试"和"要不要人工闸门"。
EFFECT_CLASSES = ("read_only", "idempotent_write", "external_side_effect")

#: 四档确定结论；`None` 表示还没结论。
OUTCOMES = ("applied", "not_applied", "partially_applied", "unknowable")


class EffectClass(str, Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class ToolCallStatus(str, Enum):
    PROPOSED = "proposed"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    FAILED_DETERMINATE = "failed_determinate"
    FAILED_PARTIAL = "failed_partial"
    INDETERMINATE = "indeterminate"
    RECONCILING = "reconciling"
    RECONCILED = "reconciled"
    COMPENSATING = "compensating"


class Outcome(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    PARTIALLY_APPLIED = "partially_applied"
    UNKNOWABLE = "unknowable"


#: §4.8 的转移表：多一条捷径都不给。
LEGAL_TOOL_CALL_TRANSITIONS: Dict[ToolCallStatus, FrozenSet[ToolCallStatus]] = {
    ToolCallStatus.PROPOSED: frozenset({ToolCallStatus.DISPATCHED}),
    ToolCallStatus.DISPATCHED: frozenset({
        ToolCallStatus.SETTLED, ToolCallStatus.FAILED_DETERMINATE,
        ToolCallStatus.FAILED_PARTIAL, ToolCallStatus.INDETERMINATE,
    }),
    # indeterminate 只能通过对账出去——绝不直接重发（重发可能造成重复副作用）
    ToolCallStatus.INDETERMINATE: frozenset({ToolCallStatus.RECONCILING}),
    ToolCallStatus.RECONCILING: frozenset({
        ToolCallStatus.RECONCILED, ToolCallStatus.INDETERMINATE,
    }),
    ToolCallStatus.RECONCILED: frozenset({ToolCallStatus.COMPENSATING}),
    ToolCallStatus.FAILED_PARTIAL: frozenset({
        ToolCallStatus.COMPENSATING, ToolCallStatus.INDETERMINATE,
    }),
    ToolCallStatus.COMPENSATING: frozenset({
        ToolCallStatus.SETTLED, ToolCallStatus.FAILED_PARTIAL,
        ToolCallStatus.INDETERMINATE,
    }),
    ToolCallStatus.SETTLED: frozenset(),
    ToolCallStatus.FAILED_DETERMINATE: frozenset({
        ToolCallStatus.DISPATCHED,  # 确定没生效 + 允许重试时才可重新派发
    }),
}


class IllegalToolCallTransition(RuntimeError):
    """当前状态不允许该目标状态；状态保持不变。"""


def can_transition(current: ToolCallStatus | str,
                   target: ToolCallStatus | str) -> bool:
    try:
        return ToolCallStatus(target) in LEGAL_TOOL_CALL_TRANSITIONS[ToolCallStatus(current)]
    except (ValueError, KeyError):
        return False


def transition(current: ToolCallStatus | str,
               target: ToolCallStatus | str) -> ToolCallStatus:
    source = ToolCallStatus(current)
    destination = ToolCallStatus(target)
    if destination not in LEGAL_TOOL_CALL_TRANSITIONS[source]:
        raise IllegalToolCallTransition(
            f"{source.value} -> {destination.value} 不是合法转移"
            f"（允许 {sorted(item.value for item in LEGAL_TOOL_CALL_TRANSITIONS[source])}）"
        )
    return destination


# 便捷判定：这些状态表示"已经有确定结论"
SETTLED_STATUSES = frozenset({
    ToolCallStatus.SETTLED, ToolCallStatus.FAILED_DETERMINATE,
    ToolCallStatus.RECONCILED,
})


def canonical_args(args: Optional[Dict[str, Any]]) -> str:
    """参数规范化：键排序、紧凑分隔、中文不转义——同一语义给同一指纹。"""
    return json.dumps(args or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def args_fingerprint(args: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_args(args).encode("utf-8")).hexdigest()[:32]


def idempotency_key(tool_name: str, args: Optional[Dict[str, Any]],
                    *, scope: str = "") -> str:
    """稳定幂等键：**不含 attempt number**（重试的是同一个逻辑操作）。

    同一语义输入 + 同一作用域 ⇒ 同一个 key，交给下游去重；
    语义变了（args 变）才是新操作、新 key（§8 的纪律）。
    """
    return f"{tool_name or 'tool'}:{scope or '-'}:{args_fingerprint(args)}"


@dataclass(frozen=True)
class ToolSpec:
    """工具能力声明；缺省一律按"最保守"处理：不支持幂等、不支持查询、副作用型。"""

    name: str
    version: str = "1"
    effect_class: EffectClass = EffectClass.EXTERNAL_SIDE_EFFECT
    supports_idempotency: bool = False
    supports_lookup: bool = False
    supports_heartbeat: bool = False
    supports_compensation: bool = False
    timeout_s: float = 30.0

    @property
    def retry_safe(self) -> bool:
        """能不能在"结果未知"之后直接重发？只有只读/幂等写且下游能去重才行。"""
        return self.supports_idempotency or self.effect_class is not EffectClass.EXTERNAL_SIDE_EFFECT


@dataclass
class ReconcileDecision:
    """对账结论：要么给出确定结果，要么明确"判不了"。"""

    status: ToolCallStatus
    outcome: Optional[Outcome] = None
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


def decide_from_observation(observed: Any, *, expected: Any = None,
                            fingerprint_matches: Optional[bool] = None) -> ReconcileDecision:
    """把一次"读回观测"翻译成对账结论。

    - 观测到动作已生效（或指纹与预期后置状态一致）→ applied；
    - 观测到目标对象还是执行前状态 → not_applied；
    - 两者都不像（部分字段变了 / 读不到）→ 继续判不了，保持 indeterminate。
    """
    if observed is None:
        return ReconcileDecision(
            status=ToolCallStatus.INDETERMINATE,
            detail="读不到目标对象，无法判断是否生效",
        )
    if expected is not None and observed == expected:
        return ReconcileDecision(
            status=ToolCallStatus.RECONCILED, outcome=Outcome.APPLIED,
            detail="观测与预期后置状态一致",
            evidence={"observed": observed},
        )
    if fingerprint_matches is True:
        return ReconcileDecision(
            status=ToolCallStatus.RECONCILED, outcome=Outcome.APPLIED,
            detail="状态指纹与执行后指纹一致",
        )
    if fingerprint_matches is False:
        return ReconcileDecision(
            status=ToolCallStatus.RECONCILED, outcome=Outcome.NOT_APPLIED,
            detail="状态指纹与执行前一致，动作未生效",
        )
    return ReconcileDecision(
        status=ToolCallStatus.INDETERMINATE,
        detail="观测与执行前/后置状态都不匹配，仍判不了",
        evidence={"observed": observed},
    )
