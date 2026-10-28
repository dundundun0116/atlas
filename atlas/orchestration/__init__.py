"""多智能体编排：固定三段式编排器 + 显式状态机 + 角色调用策略。

模块划分：
- `orchestrator`：Planner → Worker → Auditor → 一次修订的既有流程（MemoryArena 接入用）；
- `state_machine`：任务状态、合法转移、修订预算与 LangGraph 控制流表达；
- `policies`：角色调用的超时 / 有限重试 / fallback，以及 Auditor 输出的结构校验。
"""

from atlas.orchestration.orchestrator import (
    WORKER_FALLBACK_PROMPT,
    OrchestrationError,
    OrchestrationTrace,
    Orchestrator,
)
from atlas.orchestration.state_machine import (
    IllegalTransition,
    TaskState,
    TaskStateMachine,
    build_control_flow_graph,
)

__all__ = [
    "WORKER_FALLBACK_PROMPT",
    "IllegalTransition",
    "OrchestrationError",
    "OrchestrationTrace",
    "Orchestrator",
    "TaskState",
    "TaskStateMachine",
    "build_control_flow_graph",
]
