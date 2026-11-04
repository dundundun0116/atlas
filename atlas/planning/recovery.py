"""恢复评估：重启之后，**能不能接着跑、卡在哪**（设计文档 §4.13 / §5.5）。

checkpoint 里记了三样东西：还没收敛的 Step、在途的工具调用、预算快照。光记录没用，
恢复时得能据此回答三个问题：

1. 有没有"可能已经发出去了但结果未知"的外部调用？——**有就先对账，不许继续**；
2. 有没有已完成的 Step 因为上游变化被打成 `stale`？——它们要重新绑定，不能自动重跑；
3. 剩下的哪些 Step 现在就能跑？

`assess_recovery` 只读、不改状态；真正推进仍由 `PlanRunner` 负责（它自己会先把
`dispatched` 行重跑一次）。把"评估"和"执行"分开，是为了让"要不要动手"由调用方决定——
恢复是最容易造成重复副作用的动作，不该自动发生。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from atlas.planning.schemas import StepStatus

#: 这些状态的工具调用说明"外部动作可能已发生但结果未知"。
UNSETTLED_TOOL_STATUSES = ("dispatched", "indeterminate", "reconciling", "compensating")

#: 这些 Step 状态表示"还没收敛"。
UNSETTLED_STEP_STATUSES = (
    StepStatus.BLOCKED.value, StepStatus.PENDING.value, StepStatus.READY.value,
    StepStatus.DISPATCHED.value, StepStatus.RUNNING.value, StepStatus.VERIFYING.value,
    StepStatus.FAILED.value,
)


@dataclass
class RecoveryStatus:
    """一次恢复评估的结论；`blocked` 为真时**不要**自动推进。"""

    session_id: str = ""
    plan_id: str = ""
    plan_status: str = ""
    in_flight_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    pending_steps: List[str] = field(default_factory=list)
    dispatched_steps: List[str] = field(default_factory=list)
    stale_steps: List[str] = field(default_factory=list)
    needs_human_steps: List[str] = field(default_factory=list)
    failed_steps: List[str] = field(default_factory=list)
    done_steps: List[str] = field(default_factory=list)
    blocked: bool = False
    reason: str = ""
    action: str = "nothing_to_do"  # resume / blocked_on_reconcile / blocked_on_human / nothing_to_do

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "plan_status": self.plan_status,
            "in_flight_tool_calls": list(self.in_flight_tool_calls),
            "pending_steps": list(self.pending_steps),
            "dispatched_steps": list(self.dispatched_steps),
            "stale_steps": list(self.stale_steps),
            "needs_human_steps": list(self.needs_human_steps),
            "failed_steps": list(self.failed_steps),
            "done_steps": list(self.done_steps),
            "blocked": self.blocked,
            "reason": self.reason,
            "action": self.action,
        }


def assess_recovery(repository, session_id: str) -> RecoveryStatus:
    """读 checkpoint 与库里的真实状态，给出"能不能接着跑"的结论（只读）。"""
    status = RecoveryStatus(session_id=session_id)
    plan_id = repository.active_plan_id(session_id)
    if not plan_id:
        status.reason = "没有激活的计划，无法按计划恢复"
        return status
    status.plan_id = plan_id

    detail = repository.plan_detail(session_id, plan_id)
    status.plan_status = detail["plan"]["status"]

    for step in detail["steps"]:
        step_id = step["step_id"]
        state = step["status"]
        if state == StepStatus.DONE.value:
            status.done_steps.append(step_id)
        elif state == StepStatus.DISPATCHED.value:
            status.dispatched_steps.append(step_id)
        elif state == StepStatus.STALE.value:
            status.stale_steps.append(step_id)
        elif state == StepStatus.NEEDS_HUMAN.value:
            status.needs_human_steps.append(step_id)
        elif state == StepStatus.FAILED.value:
            status.failed_steps.append(step_id)
        elif state in UNSETTLED_STEP_STATUSES:
            status.pending_steps.append(step_id)

    status.in_flight_tool_calls = [
        {
            "tool_call_id": call["tool_call_id"],
            "tool_name": call["tool_name"],
            "step_id": call["step_id"],
            "status": call["status"],
            "outcome": call["outcome"],
        }
        for call in repository.tool_calls(session_id, plan_id=plan_id)
        if call.get("status") in UNSETTLED_TOOL_STATUSES
    ]

    # 优先级：在途外部调用 > 人工闸门 > 可续跑 > 没得做
    if status.in_flight_tool_calls:
        status.blocked = True
        status.action = "blocked_on_reconcile"
        status.reason = (
            f"{len(status.in_flight_tool_calls)} 个工具调用结果未知，"
            "必须先对账（重复发可能造成重复副作用）"
        )
    elif status.needs_human_steps:
        status.blocked = True
        status.action = "blocked_on_human"
        status.reason = f"{len(status.needs_human_steps)} 个 Step 需要人工确认"
    elif status.dispatched_steps or status.pending_steps or status.failed_steps:
        status.action = "resume"
        status.reason = "可以接着跑：只补未提交的那一步与未完成的 Step"
    elif status.stale_steps:
        status.action = "blocked_on_human"
        status.blocked = True
        status.reason = (
            f"{len(status.stale_steps)} 个 Step 因上游变化失效，需要重新绑定输入后再跑"
        )
    elif len(status.done_steps) == len(detail["steps"]):
        status.action = "nothing_to_do"
        status.reason = "所有 Step 已完成"
    else:
        status.action = "nothing_to_do"
        status.reason = "没有可推进的 Step"
    return status
