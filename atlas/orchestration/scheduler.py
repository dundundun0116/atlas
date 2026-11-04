"""确定性 Scheduler：读快照、返回决定，不调模型也不调工具（设计文档 §5.3）。

三个纯函数：
- `ready_steps(...)`   —— 谁现在可以跑；
- `select_next(...)`   —— 只派发一个（首版串行，§5.3 明确不引入并发）；
- `affected_steps(...)` —— 某个输入变了，下游哪些 step 要打成 stale。

纪律：
- 只读入参，不查库、不开事务；状态 claim 由 repository 的条件更新负责；
- 排序稳定（依赖层级 → ordinal → step_id），同一快照永远给出同一个决定，
  否则"可复现的恢复"无从谈起；
- 依赖未完成的 Step 一律不进 ready，无论它本身多"想跑"。
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from atlas.planning.schemas import DependencyKind, PlanSpec, StepStatus, steps_in_ordinal_order

#: 只有这些状态允许被派发（其余要么已在跑，要么已收敛）。
DISPATCHABLE_STATUSES = frozenset({StepStatus.PENDING, StepStatus.READY})

#: 满足这些状态才算"上游这一步已经给出可用结果"。
SATISFIED_STATUSES = frozenset({StepStatus.DONE, StepStatus.SKIPPED})


@dataclass
class DispatchDecision:
    """一次调度决定；`step_id` 为空表示"现在没有可跑的"。"""

    step_id: str = ""
    reason: str = ""
    blocked_by: List[str] = field(default_factory=list)

    @property
    def dispatched(self) -> bool:
        return bool(self.step_id)

    def to_dict(self) -> Dict[str, object]:
        return {"step_id": self.step_id, "reason": self.reason,
                "blocked_by": list(self.blocked_by)}


def _dependencies_of(plan: PlanSpec, orders: Optional[Mapping[str, List[str]]] = None
                     ) -> Dict[str, List[str]]:
    """下游 step_id → 它依赖的上游 step_id 列表（按 plan 的 dependencies 展开）。"""
    if orders is not None:
        return {step_id: list(parents) for step_id, parents in orders.items()}
    deps: Dict[str, List[str]] = {step.step_id: [] for step in plan.steps}
    for dependency in plan.dependencies:
        deps.setdefault(dependency.to_step_id, []).append(dependency.from_step_id)
    return deps


def dependency_levels(plan: PlanSpec) -> Dict[str, int]:
    """每个 step 的依赖层级（从根算起的最长路径）；用于稳定排序与并发判断。"""
    deps = _dependencies_of(plan)
    levels: Dict[str, int] = {}

    def level(step_id: str, seen: Set[str]) -> int:
        if step_id in levels:
            return levels[step_id]
        if step_id in seen:  # 环：由 validator 拦，这里不递归爆栈
            return 0
        parents = deps.get(step_id, [])
        value = 0 if not parents else max(level(parent, seen | {step_id}) + 1 for parent in parents)
        levels[step_id] = value
        return value

    for step in plan.steps:
        level(step.step_id, set())
    return levels


def unblockable_steps(
    plan: PlanSpec,
    statuses: Mapping[str, StepStatus | str],
) -> List[str]:
    """依赖已全部满足、但自身还停在 blocked 的 step（下一次 tick 会转成 pending）。"""
    deps = _dependencies_of(plan)
    result: List[str] = []
    for step in steps_in_ordinal_order(plan.steps):
        if StepStatus(statuses.get(step.step_id, StepStatus.BLOCKED)) is not StepStatus.BLOCKED:
            continue
        parents = deps.get(step.step_id, [])
        if all(
            StepStatus(statuses.get(parent, StepStatus.BLOCKED)) in SATISFIED_STATUSES
            for parent in parents
        ):
            result.append(step.step_id)
    return result


def ready_steps(
    plan: PlanSpec,
    statuses: Mapping[str, StepStatus | str],
    *,
    preconditions: Optional[Mapping[str, Optional[bool]]] = None,
) -> List[str]:
    """**本次 tick 之后**可以派发的 step（按依赖层级 + ordinal 的稳定顺序）。

    依据 §4.4 的三段语义：`blocked` 依赖未完成 → 依赖满足即变 `pending` →
    前置条件为真才变 `ready`。所以这里把"依赖已满足的 blocked"也算进来
    （否则调用方还得自己跑一遍 unblock，纯函数就没法单独用）。

    前置条件：`False` 排除；键存在但为 `None` 表示真值**未知** ⇒ 不自动派发
    （§5.4 第 5 条：unknown 必须被探测覆盖或转人工）。
    数据绑定能否解析、预算是否预留由调用方在此之前确认。
    """
    deps = _dependencies_of(plan)
    levels = dependency_levels(plan)
    order = {step.step_id: step.ordinal for step in steps_in_ordinal_order(plan.steps)}
    checks = dict(preconditions or {})
    unblockable = set(unblockable_steps(plan, statuses))

    ready: List[str] = []
    for step in plan.steps:
        step_id = step.step_id
        status = StepStatus(statuses.get(step_id, StepStatus.BLOCKED))
        if status is StepStatus.BLOCKED and step_id not in unblockable:
            continue
        if status is not StepStatus.BLOCKED and status not in DISPATCHABLE_STATUSES:
            continue
        parents = deps.get(step_id, [])
        if any(
            StepStatus(statuses.get(parent, StepStatus.BLOCKED)) not in SATISFIED_STATUSES
            for parent in parents
        ):
            continue
        if step_id in checks:
            if checks[step_id] is None:
                continue  # 真值未知 ⇒ 不自动派发
            if checks[step_id] is False:
                continue
        ready.append(step_id)
    ready.sort(key=lambda item: (levels.get(item, 0), order.get(item, 0), item))
    return ready


@dataclass
class TickResult:
    """一次调度 tick 的完整建议：先把该 unblock 的放出来，再派发一个。"""

    to_pending: List[str] = field(default_factory=list)
    ready: List[str] = field(default_factory=list)
    decision: DispatchDecision = field(default_factory=DispatchDecision)

    @property
    def step_id(self) -> str:
        return self.decision.step_id

    def to_dict(self) -> Dict[str, object]:
        return {
            "to_pending": list(self.to_pending),
            "ready": list(self.ready),
            "decision": self.decision.to_dict(),
        }


def plan_tick(
    plan: PlanSpec,
    statuses: Mapping[str, StepStatus | str],
    *,
    preconditions: Optional[Mapping[str, Optional[bool]]] = None,
    paused: bool = False,
    budget_exhausted: bool = False,
) -> TickResult:
    """调度器一拍：返回"该转 pending 的"、"可派发的"和"这一拍选谁"。"""
    pending = unblockable_steps(plan, statuses)
    ready = ready_steps(plan, statuses, preconditions=preconditions)
    decision = select_next(
        ready, plan=plan, statuses=statuses,
        paused=paused, budget_exhausted=budget_exhausted,
    )
    return TickResult(to_pending=pending, ready=ready, decision=decision)



def select_next(
    ready: Sequence[str],
    *,
    plan: Optional[PlanSpec] = None,
    statuses: Optional[Mapping[str, StepStatus | str]] = None,
    paused: bool = False,
    budget_exhausted: bool = False,
) -> DispatchDecision:
    """首版串行：一次只挑一个（§5.3 "首个版本不需要并发调度"）。"""
    if paused:
        return DispatchDecision(reason="run 处于暂停/等待中，不派发")
    if budget_exhausted:
        return DispatchDecision(reason="预算已耗尽，不派发新 step")
    if not ready:
        blocked = []
        if plan is not None and statuses is not None:
            blocked = _blocked_step_ids(plan, statuses)
        return DispatchDecision(reason="当前没有可派发的 step", blocked_by=blocked)
    chosen = ready[0]
    return DispatchDecision(step_id=chosen, reason=f"串行调度：{chosen} 在稳定序中最早")


def _blocked_step_ids(plan: PlanSpec, statuses: Mapping[str, StepStatus | str]) -> List[str]:
    """还没收敛、但依赖没满足的 step（用于说明"为什么没得跑"）。"""
    deps = _dependencies_of(plan)
    blocked: List[str] = []
    for step in plan.steps:
        status = StepStatus(statuses.get(step.step_id, StepStatus.BLOCKED))
        if status in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.CANCELLED):
            continue
        parents = deps.get(step.step_id, [])
        waiting = [
            parent for parent in parents
            if StepStatus(statuses.get(parent, StepStatus.BLOCKED)) not in SATISFIED_STATUSES
        ]
        if waiting:
            blocked.append(step.step_id)
    return blocked


def affected_steps(
    changed_step_ids: Iterable[str],
    dependencies: Optional[Sequence[tuple]] = None,
    *,
    plan: Optional[PlanSpec] = None,
) -> Set[str]:
    """输入变化的传播闭包：被改动的 step 及其全部下游（不含自身）。

    `dependencies` 可以是 `[(from_step_id, to_step_id), ...]`，也可以直接给 plan。
    这是"局部重规划"与"把下游打成 stale"共用的原语（§5.5 / §5.6）。
    """
    edges: List[tuple] = list(dependencies or [])
    if plan is not None:
        edges = [(item.from_step_id, item.to_step_id) for item in plan.dependencies]
    downstream: Dict[str, List[str]] = {}
    for source, target in edges:
        downstream.setdefault(source, []).append(target)

    affected: Set[str] = set()
    queue = list(changed_step_ids)
    while queue:
        node = queue.pop()
        for target in downstream.get(node, []):
            if target in affected:
                continue
            affected.add(target)
            queue.append(target)
    return affected


def stale_after_change(
    changed_step_ids: Iterable[str],
    statuses: Mapping[str, StepStatus | str],
    dependencies: Optional[Sequence[tuple]] = None,
    *,
    plan: Optional[PlanSpec] = None,
) -> List[str]:
    """哪些**已经完成**的下游 step 因为上游变化而失效（要转 stale 的那批）。"""
    affected = affected_steps(changed_step_ids, dependencies, plan=plan)
    return sorted(
        step_id for step_id in affected
        if StepStatus(statuses.get(step_id, StepStatus.BLOCKED)) is StepStatus.DONE
    )


def data_dependencies(plan: PlanSpec) -> List[tuple]:
    """只看 data 边：只有它携带"上游输出变了，下游输入就变了"的语义。"""
    return [
        (item.from_step_id, item.to_step_id)
        for item in plan.dependencies
        if item.kind is DependencyKind.DATA
    ]
