"""Step 状态机：十二个状态、一张合法转移表（设计文档 §5.2）。

纪律与 Run/任务状态机一致：
- 状态只能经**本模块的函数**改变，不允许直接 ORM 更新绕过（设计文档 §5.2 最后一句）；
- 非法转移不静默：抛 `IllegalStepTransition`，状态不变；
- 转移表是唯一的真源，`TransitionRecord` 只是它的可读投影。

语义要点（§4.4）：
- `done` 只能由 `verifying` 到达——"模型说完了"不等于完成；
- `stale` 可以从 `ready`（还没跑）与 `done`（跑完但上游变了）到达；
- `stale -> pending` 是新计划重新绑定输入后的复活路径。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, List, Optional

from atlas.planning.schemas import StepStatus


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


#: 合法转移表：from → 允许到达的 to 集合。
LEGAL_STEP_TRANSITIONS: Dict[StepStatus, FrozenSet[StepStatus]] = {
    StepStatus.BLOCKED: frozenset({StepStatus.PENDING, StepStatus.CANCELLED}),
    StepStatus.PENDING: frozenset({
        StepStatus.READY, StepStatus.NEEDS_HUMAN, StepStatus.SKIPPED, StepStatus.CANCELLED,
    }),
    StepStatus.READY: frozenset({
        StepStatus.DISPATCHED, StepStatus.STALE, StepStatus.NEEDS_HUMAN, StepStatus.CANCELLED,
    }),
    StepStatus.DISPATCHED: frozenset({
        StepStatus.RUNNING, StepStatus.FAILED, StepStatus.INDETERMINATE, StepStatus.CANCELLED,
    }),
    StepStatus.RUNNING: frozenset({
        StepStatus.VERIFYING, StepStatus.FAILED, StepStatus.INDETERMINATE,
        StepStatus.NEEDS_HUMAN, StepStatus.CANCELLED,
    }),
    StepStatus.VERIFYING: frozenset({
        StepStatus.DONE, StepStatus.READY, StepStatus.FAILED,
        StepStatus.NEEDS_HUMAN, StepStatus.STALE,
    }),
    StepStatus.FAILED: frozenset({StepStatus.READY, StepStatus.NEEDS_HUMAN, StepStatus.CANCELLED}),
    StepStatus.DONE: frozenset({StepStatus.STALE}),
    StepStatus.STALE: frozenset({StepStatus.PENDING, StepStatus.CANCELLED}),
    StepStatus.NEEDS_HUMAN: frozenset({
        StepStatus.PENDING, StepStatus.READY, StepStatus.CANCELLED,
    }),
    StepStatus.SKIPPED: frozenset({StepStatus.PENDING}),
    StepStatus.CANCELLED: frozenset(),
}

#: 终态：不会再自发变化（`done` 仍可能被上游变化打成 `stale`，所以不算终态）。
STEP_TERMINAL_STATES: FrozenSet[StepStatus] = frozenset({StepStatus.CANCELLED})


class IllegalStepTransition(RuntimeError):
    """当前状态不允许该目标状态；状态保持不变。"""


@dataclass
class StepTransitionRecord:
    from_status: str
    to_status: str
    reason: str
    at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "from": self.from_status,
            "to": self.to_status,
            "reason": self.reason,
            "at": self.at,
        }


def can_transition(current: StepStatus | str, target: StepStatus | str) -> bool:
    try:
        return StepStatus(target) in LEGAL_STEP_TRANSITIONS[StepStatus(current)]
    except (ValueError, KeyError):
        return False


def transition(current: StepStatus | str, target: StepStatus | str,
               reason: str = "") -> StepTransitionRecord:
    """校验并返回一条转移记录；非法则抛 `IllegalStepTransition`。"""
    try:
        source = StepStatus(current)
        destination = StepStatus(target)
    except ValueError as exc:
        raise IllegalStepTransition(f"未知 Step 状态：{exc}") from exc
    allowed = LEGAL_STEP_TRANSITIONS.get(source, frozenset())
    if destination not in allowed:
        raise IllegalStepTransition(
            f"{source.value} -> {destination.value} 不是合法转移"
            f"（允许 {sorted(item.value for item in allowed)}）"
        )
    return StepTransitionRecord(
        from_status=source.value, to_status=destination.value,
        reason=reason, at=_now(),
    )


def allowed_next(current: StepStatus | str) -> List[str]:
    """当前状态可去往的状态（UI 只列这些，避免与状态机漂移）。"""
    try:
        return sorted(item.value for item in LEGAL_STEP_TRANSITIONS[StepStatus(current)])
    except (ValueError, KeyError):
        return []


def initial_status(has_unfinished_dependencies: bool) -> StepStatus:
    """新计划落库时的初始状态：有未完成依赖就 blocked，否则 pending。"""
    return StepStatus.BLOCKED if has_unfinished_dependencies else StepStatus.PENDING


def is_settled(status: StepStatus | str) -> bool:
    """这一步是否已经"不再需要调度器管"（done / skipped / cancelled）。"""
    return StepStatus(status) in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.CANCELLED)


def failure_status_for(kind: str) -> StepStatus:
    """失败分类 → Step 状态：能确定没生效就 failed，结果未知就必须进 indeterminate。"""
    normalised = str(kind or "").strip().lower()
    if normalised in ("unknown_commit", "indeterminate", "timeout_after_dispatch"):
        return StepStatus.INDETERMINATE
    return StepStatus.FAILED


def next_status_after_attempt(attempt_status: str,
                              verified: Optional[bool]) -> StepStatus:
    """一次 Attempt 结束后 Step 该去哪：未验证不算 done（§4.4 语义约定）。"""
    normalised = str(attempt_status or "").strip().lower()
    if normalised in ("indeterminate", "unknown_commit"):
        return StepStatus.INDETERMINATE
    if normalised in ("failed", "interrupted"):
        return StepStatus.FAILED
    if verified is True:
        return StepStatus.DONE
    if verified is False:
        return StepStatus.FAILED
    # 结果拿回来了但没有可执行判据：只能转人工，不能当成功
    return StepStatus.NEEDS_HUMAN
