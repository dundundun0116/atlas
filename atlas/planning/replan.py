"""局部重规划：环境变了，只让**相关**步骤失效（设计文档 §5.6 / §11 Phase 4）。

整计划重写在长任务里代价太大：一处外部对象变了，把十步全部作废，等于把做对的部分也扔了。
这里要回答的是精确的三个问题——

1. **触发信号是什么**？（哪个对象变了、旧指纹是什么、新指纹是什么）
2. **影响范围到哪**？（被改动的 step 及其下游闭包；不相关分支一个都不动）
3. **哪些步骤可以复用**？（已完成且不在闭包里的——它们的结果仍然有效）

`PlanningDecision` 就是这三个问题的答案，也是"每次重规划能解释"的载体。
它只做判定与描述；真正落库（把下游打成 stale、写事件）由调用方用既有 repository 接口完成。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from atlas.environment.observations import EnvChange
from atlas.orchestration.scheduler import affected_steps, stale_after_change
from atlas.planning.schemas import PlanSpec, StepStatus

#: 触发信号种类。写清楚比"反正变了就重规划"有用得多。
TRIGGERS = ("environment_changed", "step_failed", "manual", "upstream_revised")

#: 已完成（结果仍然有效）的状态。
_DONE = (StepStatus.DONE.value, "done")


@dataclass
class PlanningDecision:
    """一次重规划的判定结果——可解释是硬要求（§11 验收）。"""

    trigger: str
    changed_steps: List[str] = field(default_factory=list)
    affected_steps: List[str] = field(default_factory=list)
    stale_steps: List[str] = field(default_factory=list)
    reused_steps: List[str] = field(default_factory=list)
    changes: List[EnvChange] = field(default_factory=list)
    reason: str = ""

    @property
    def requires_replan(self) -> bool:
        return bool(self.stale_steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trigger": self.trigger,
            "changed_steps": list(self.changed_steps),
            "affected_steps": list(self.affected_steps),
            "stale_steps": list(self.stale_steps),
            "reused_steps": list(self.reused_steps),
            "changes": [item.to_dict() if hasattr(item, "to_dict") else dict(item)
                        for item in self.changes],
            "reason": self.reason,
            "requires_replan": self.requires_replan,
        }

    def summary(self) -> str:
        """一句话解释：触发是什么、动了哪几步、哪些保持有效。"""
        head = {
            "environment_changed": "外部环境变化",
            "step_failed": "上游步骤失败",
            "manual": "人工触发",
            "upstream_revised": "上游结果修订",
        }.get(self.trigger, self.trigger)
        changed = "、".join(self.changed_steps) or "—"
        stale = "、".join(self.stale_steps) or "无"
        reused = "、".join(self.reused_steps) or "无"
        return (f"{head}（改动步骤 {changed}）⇒ 失效 {stale}；"
                f"保持有效 {reused}。{self.reason}".strip())


def decide_replan(
    plan: PlanSpec,
    statuses: Mapping[str, Any],
    changed_step_ids: Iterable[str],
    *,
    trigger: str = "environment_changed",
    changes: Optional[Sequence[EnvChange]] = None,
    reason: str = "",
) -> PlanningDecision:
    """算出"这次变化该让哪些步骤失效、哪些可以复用"。

    注意 **不复用 `affected_steps` 当结论**：
    受影响闭包里的步骤如果有没完成的，它不需要"失效"（本来就要重跑）；
    只有**已完成**的那些才需要转 stale。这两件事分开列，报告才解释得清。
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"未知触发类型 {trigger!r}（可选 {TRIGGERS}）")
    changed = [str(item) for item in changed_step_ids]
    edges = [(item.from_step_id, item.to_step_id) for item in plan.dependencies]
    closure: Set[str] = affected_steps(changed, edges)
    stale = stale_after_change(changed, statuses, edges)

    done = {step_id for step_id, status in statuses.items()
            if str(getattr(status, "value", status)) in _DONE}
    reused = sorted(done - closure - set(changed))

    if not changed and not stale:
        fallback = reason or "没有需要失效的已完成步骤"
    else:
        fallback = reason
    return PlanningDecision(
        trigger=trigger,
        changed_steps=changed,
        affected_steps=sorted(closure),
        stale_steps=list(stale),
        reused_steps=list(reused),
        changes=list(changes or []),
        reason=fallback,
    )


def steps_touching_targets(plan: PlanSpec, targets: Iterable[str]) -> List[str]:
    """哪些步骤**直接**操作了这些对象（环境变化 → 改动步骤的翻译层）。

    只认 `tool_args_template` 里的 `target`：这是写操作计划里唯一稳定的约定
    （见 `atlas.planning.write_workflow`）。认不出来就不硬猜——宁可少标，
    也不能把不相关分支打掉（那正是本模块要避免的事）。
    """
    wanted = {str(item) for item in targets}
    hits: List[str] = []
    for step in plan.steps:
        template = step.tool_args_template or {}
        target = str(template.get("target") or "").strip()
        if target and target in wanted:
            hits.append(step.step_id)
    return hits


def apply_replan(
    repository,
    session_id: str,
    plan_id: str,
    decision: PlanningDecision,
    *,
    actor: str = "scheduler",
) -> Dict[str, Any]:
    """把判定落库：已完成的下游转 stale（事件带原因）。返回落库后的摘要。"""
    marked: List[str] = []
    if decision.stale_steps:
        marked = repository.mark_downstream_stale(
            session_id, plan_id, decision.changed_steps,
            reason=decision.summary(),
        )
    return {
        "stale_steps": list(marked),
        "reused_steps": list(decision.reused_steps),
        "decision": decision.to_dict(),
    }
