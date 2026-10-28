"""显式任务状态机：7 个状态、合法转移、修订预算与检查点快照。

设计要点（计划阶段 5）：
- 状态与转移是**确定性代码**，模型只能通过触发词推进流程；
- 非法转移不静默忽略：写入 `violations`（可接审计事件）后抛 `IllegalTransition`，
  状态不变；
- 修订预算硬上限：超出预算直接进 FAILED 并留下原因，避免 Auditor 反复打回死循环；
- `to_checkpoint()/from_checkpoint()` 与 repository 的 checkpoint 字段对接，
  恢复数据永远进 Atlas 自己的存储，不用框架 checkpoint 当第二真源；
- LangGraph 只在 `build_control_flow_graph()` 里表达控制流，节点内部仍调本状态机。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskState(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    AUDITING = "auditing"
    REPAIRING = "repairing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATES = (
    TaskState.PLANNING,
    TaskState.EXECUTING,
    TaskState.AUDITING,
    TaskState.REPAIRING,
)
TERMINAL_STATES = (TaskState.COMPLETED, TaskState.FAILED)

#: 合法转移表（验收：非法转移必须被拒绝，且不能有隐式状态丢失）。
LEGAL_TRANSITIONS: Dict[TaskState, FrozenSet[TaskState]] = {
    TaskState.PLANNING: frozenset({TaskState.EXECUTING, TaskState.PAUSED, TaskState.FAILED}),
    TaskState.EXECUTING: frozenset({TaskState.AUDITING, TaskState.PAUSED, TaskState.FAILED}),
    TaskState.AUDITING: frozenset(
        {TaskState.COMPLETED, TaskState.REPAIRING, TaskState.PAUSED, TaskState.FAILED}
    ),
    TaskState.REPAIRING: frozenset(
        {TaskState.AUDITING, TaskState.PAUSED, TaskState.FAILED}
    ),
    # 暂停后可以恢复到任意进行中状态（具体回到哪个由 checkpoint 记录）
    TaskState.PAUSED: frozenset(ACTIVE_STATES) | frozenset({TaskState.FAILED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
}

#: 触发词 → 目标状态（resume 例外：回到暂停前的状态）。
TRIGGER_TARGETS: Dict[str, Optional[TaskState]] = {
    "plan_ready": TaskState.EXECUTING,
    "work_done": TaskState.AUDITING,
    "audit_pass": TaskState.COMPLETED,
    "audit_repair": TaskState.REPAIRING,
    "audit_fatal": TaskState.FAILED,
    "repair_done": TaskState.AUDITING,
    "pause": TaskState.PAUSED,
    "resume": None,
    "fail": TaskState.FAILED,
}


class IllegalTransition(RuntimeError):
    """当前状态不允许该触发词；状态保持不变，尝试已记入 violations。"""


@dataclass
class TransitionRecord:
    from_state: str
    to_state: str
    trigger: str
    reason: str
    at: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "trigger": self.trigger,
            "reason": self.reason,
            "at": self.at,
        }

    def __getitem__(self, key: str) -> str:
        return self.to_dict()[key]


class TaskStateMachine:
    """一个任务的状态机实例；历史与违规都留在内存，可序列化进 checkpoint。"""

    def __init__(
        self,
        initial: TaskState = TaskState.PLANNING,
        max_repairs: int = 1,
        audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        checkpoint_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        budget=None,
    ):
        if max_repairs < 0:
            raise ValueError("max_repairs 不能为负")
        self.state = TaskState(initial)
        self.max_repairs = max_repairs
        self.repairs = 0
        self.failure_reason = ""
        self._paused_from: Optional[TaskState] = None
        self.history: List[TransitionRecord] = []
        self.violations: List[Dict[str, Any]] = []
        self.budget = budget
        self._audit_hook = audit_hook
        # 每次成功转移后回调一次快照：用于"转移即落 checkpoint"（阶段 C）
        self._checkpoint_sink = checkpoint_sink

    # ---------- 查询 ----------

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def can(self, trigger: str) -> bool:
        try:
            self._resolve(trigger)
        except (IllegalTransition, ValueError):
            return False
        return True

    def _resolve(self, trigger: str) -> TaskState:
        if trigger not in TRIGGER_TARGETS:
            raise ValueError(f"未知触发词：{trigger}（可选 {sorted(TRIGGER_TARGETS)}）")
        if self.is_terminal:
            raise IllegalTransition(f"终态 {self.state.value} 不再接受触发词 {trigger}")
        if trigger == "resume":
            if self._paused_from is None:
                raise IllegalTransition("没有可恢复的暂停前状态")
            target = self._paused_from
        else:
            target = TRIGGER_TARGETS[trigger]
        if target not in LEGAL_TRANSITIONS[self.state]:
            raise IllegalTransition(
                f"{self.state.value} --{trigger}--> {target.value} 不是合法转移"
            )
        return target

    # ---------- 推进 ----------

    def apply(self, trigger: str, reason: str = "") -> TaskState:
        try:
            target = self._resolve(trigger)
        except IllegalTransition:
            self.record_violation(trigger, reason)
            raise
        if trigger == "audit_repair":
            limit = self.budget.max_repairs if self.budget is not None else self.max_repairs
            if self.repairs >= limit:
                # 预算用尽：不再进入 REPAIRING，直接收敛到 FAILED 并留原因
                target = TaskState.FAILED
                reason = (
                    f"repair_budget_exhausted: 已修订 {self.repairs} 次，"
                    f"上限 {limit}；{reason}"
                ).strip()
            else:
                self.repairs += 1
                if self.budget is not None:
                    self.budget.spend_repair()
        if trigger == "pause":
            self._paused_from = self.state
        elif trigger == "resume":
            self._paused_from = None
        if target is TaskState.FAILED:
            self.failure_reason = reason

        record = TransitionRecord(
            from_state=self.state.value,
            to_state=target.value,
            trigger=trigger,
            reason=reason,
            at=_now(),
        )
        self.state = target
        self.history.append(record)
        if self._audit_hook is not None:
            self._audit_hook("transition", record.to_dict())
        if self._checkpoint_sink is not None:
            self._checkpoint_sink(self.to_checkpoint())
        return self.state

    # ---------- 失败与审计 ----------

    def record_violation(self, trigger: str, reason: str = "") -> Dict[str, Any]:
        violation = {
            "from": self.state.value,
            "trigger": trigger,
            "reason": reason,
            "at": _now(),
        }
        self.violations.append(violation)
        if self._audit_hook is not None:
            self._audit_hook("illegal_transition", violation)
        return violation

    # ---------- 快照 ----------

    def to_checkpoint(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "paused_from": self._paused_from.value if self._paused_from else None,
            "repairs": self.repairs,
            "max_repairs": self.max_repairs,
            "failure_reason": self.failure_reason,
            "history_len": len(self.history),
            "history": [r.to_dict() for r in self.history],
        }

    @classmethod
    def from_checkpoint(cls, data: Dict[str, Any]) -> "TaskStateMachine":
        machine = cls(
            initial=TaskState(data.get("state", TaskState.PLANNING.value)),
            max_repairs=int(data.get("max_repairs", 1)),
        )
        machine.repairs = int(data.get("repairs", 0))
        machine.failure_reason = str(data.get("failure_reason", ""))
        paused = data.get("paused_from")
        machine._paused_from = TaskState(paused) if paused else None
        for record in data.get("history", []):
            machine.history.append(
                TransitionRecord(
                    from_state=record.get("from", ""),
                    to_state=record.get("to", ""),
                    trigger=record.get("trigger", ""),
                    reason=record.get("reason", ""),
                    at=record.get("at", ""),
                )
            )
        return machine


def build_control_flow_graph(machine: TaskStateMachine, handlers: Dict[str, Callable[[], str]]):
    """用 LangGraph 表达 Planner→Worker→Auditor→Repair 的控制流。

    每个节点只做两件事：调 `handlers[state]()` 拿到触发词，然后交给状态机推进。
    状态（图、事件、checkpoint）仍然只由 Atlas repository 管理 —— 这里不挂
    LangGraph 的 checkpointer，避免业务状态出现第二真源。
    """
    from langgraph.graph import END, StateGraph

    nodes = {state.value for state in ACTIVE_STATES if state.value in handlers}
    if not nodes:
        raise ValueError("handlers 至少要覆盖一个进行中状态")

    def make_node(name: str):
        def run(_: Dict[str, str]) -> Dict[str, str]:
            trigger = handlers[name]()
            if trigger:
                machine.apply(trigger, reason=f"{name} 步骤")
            return {"state": machine.state.value}

        return run

    graph = StateGraph(dict)
    for name in nodes:
        graph.add_node(name, make_node(name))

    entry = machine.state.value if machine.state.value in nodes else "planning"
    if entry not in nodes:
        raise ValueError(f"入口状态 {entry} 没有对应的 handler")
    graph.set_entry_point(entry)
    mapping = {state.value: (state.value if state.value in nodes else END) for state in TaskState}
    for name in nodes:
        graph.add_conditional_edges(name, lambda payload: payload["state"], mapping)
    return graph.compile()
