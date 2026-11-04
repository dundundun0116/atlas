"""多智能体编排：固定三段式编排器 + 两套状态机 + 确定性调度。

模块划分：
- `orchestrator`：Planner → Worker → Auditor → 一次修订的既有流程（`legacy_research_v1`）；
- `state_machine`：**任务**状态、合法转移、修订预算与 LangGraph 控制流表达；
- `step_machine`：**Step** 十三态状态机（`done` 只能由 `verifying` 到达）；
- `scheduler`：纯函数调度（ready / select / affected），不调模型也不调工具；
- `policies`：角色调用的超时 / 有限重试 / fallback，以及 Auditor 输出的结构校验。
"""

from atlas.orchestration.orchestrator import (
    WORKER_FALLBACK_PROMPT,
    OrchestrationError,
    OrchestrationTrace,
    Orchestrator,
)
from atlas.orchestration.scheduler import (
    DispatchDecision,
    TickResult,
    affected_steps,
    plan_tick,
    ready_steps,
    select_next,
    stale_after_change,
    unblockable_steps,
)
from atlas.orchestration.state_machine import (
    IllegalTransition,
    TaskState,
    TaskStateMachine,
    build_control_flow_graph,
)
from atlas.orchestration.step_machine import (
    IllegalStepTransition,
    allowed_next,
    can_transition,
    failure_status_for,
    initial_status,
    next_status_after_attempt,
    transition,
)

__all__ = [
    "WORKER_FALLBACK_PROMPT",
    "DispatchDecision",
    "IllegalStepTransition",
    "IllegalTransition",
    "OrchestrationError",
    "OrchestrationTrace",
    "Orchestrator",
    "TaskState",
    "TaskStateMachine",
    "TickResult",
    "affected_steps",
    "allowed_next",
    "build_control_flow_graph",
    "can_transition",
    "failure_status_for",
    "initial_status",
    "next_status_after_attempt",
    "plan_tick",
    "ready_steps",
    "select_next",
    "stale_after_change",
    "transition",
    "unblockable_steps",
]
