"""事件定义与回放：把「状态为什么会变」写成不可变事实。

事件是状态变化的唯一叙事来源：`replay()` 能只凭事件重建关键状态变化，
因此审计与 replay 不依赖某次运行的内存对象。
"""

from typing import Any, Dict, Iterable, List

from atlas.memory.schema import EdgeType, NodeStatus
from atlas.storage.models import EventRow

ADD_NODE = "add_node"
LINK = "link"
SUPERSEDE = "supersede"
EXPIRE = "expire"
COMPLETE_SUBTASK = "complete_subtask"

OPERATIONS = (ADD_NODE, LINK, SUPERSEDE, EXPIRE, COMPLETE_SUBTASK)


def node_added(node_id: str, type_: str, content: str, actor: str) -> Dict[str, Any]:
    return {"node_id": node_id, "type": type_, "content": content, "actor": actor}


def edge_linked(src: str, dst: str, type_: EdgeType) -> Dict[str, Any]:
    return {"src": src, "dst": dst, "type": EdgeType(type_).value}


def node_superseded(old: str, new: str, reason: str = "") -> Dict[str, Any]:
    return {"node_id": old, "superseded_by": new, "reason": reason}


def node_expired(node_id: str, reason: str = "") -> Dict[str, Any]:
    return {"node_id": node_id, "reason": reason}


def subtask_completed(node_id: str) -> Dict[str, Any]:
    return {"node_id": node_id}


def replay(events: Iterable[EventRow]) -> Dict[str, Dict[str, Any]]:
    """只凭事件重建每个节点的生命周期状态。

    返回 `{node_id: {"status": ..., "superseded_by": ...}}`；
    `link` 不改变节点状态，因此不参与重建（但仍在事件流里）。
    """
    states: Dict[str, Dict[str, Any]] = {}
    for event in sorted(events, key=lambda e: e.seq):
        payload = event.payload or {}
        if event.operation == ADD_NODE:
            states[payload["node_id"]] = {
                "status": NodeStatus.ACTIVE.value,
                "superseded_by": None,
                "note": "",
            }
        elif event.operation == SUPERSEDE:
            target = states.setdefault(
                payload["node_id"], {"status": NodeStatus.ACTIVE.value, "superseded_by": None, "note": ""}
            )
            target["status"] = NodeStatus.SUPERSEDED.value
            target["superseded_by"] = payload.get("superseded_by")
            target["note"] = payload.get("reason", "")
        elif event.operation == EXPIRE:
            target = states.setdefault(
                payload["node_id"], {"status": NodeStatus.ACTIVE.value, "superseded_by": None, "note": ""}
            )
            target["status"] = NodeStatus.EXPIRED.value
            target["note"] = payload.get("reason", "")
        elif event.operation == COMPLETE_SUBTASK:
            target = states.setdefault(
                payload["node_id"], {"status": NodeStatus.ACTIVE.value, "superseded_by": None, "note": ""}
            )
            target["status"] = NodeStatus.DONE.value
    return states


def operations_of(events: Iterable[EventRow]) -> List[str]:
    return [e.operation for e in sorted(events, key=lambda e: e.seq)]


def replay_with_problems(events: Iterable[EventRow]):
    """回放并检测事件序列损坏（Task 4）：返回 (状态表, 问题列表)。

    检测三类损坏：
    - `seq` 重复或回退（事件表本应单调）；
    - `supersede` 指向未知节点；
    - 已进入终态的节点又被变更（superseded/expired/done 不允许再改）。
    """
    problems: List[str] = []
    ordered = sorted(events, key=lambda e: (e.seq, e.event_id))
    previous_seq = 0
    for event in ordered:
        if event.seq <= previous_seq:
            problems.append(f"seq 非单调：{event.seq} 出现在 {previous_seq} 之后")
        previous_seq = max(previous_seq, event.seq)

    states: Dict[str, Dict[str, Any]] = {}
    for event in ordered:
        payload = event.payload or {}
        operation = event.operation
        if operation == ADD_NODE:
            node_id = payload.get("node_id")
            if node_id in states:
                problems.append(f"节点 {node_id} 重复创建")
            states[node_id] = {"status": NodeStatus.ACTIVE.value, "superseded_by": None, "note": ""}
            continue
        if operation == LINK:
            for key in ("src", "dst"):
                node_id = payload.get(key)
                if node_id not in states:
                    problems.append(f"边引用未知节点：{node_id}")
            continue
        if operation not in (SUPERSEDE, EXPIRE, COMPLETE_SUBTASK):
            continue
        node_id = payload.get("node_id")
        state = states.get(node_id)
        if state is None:
            problems.append(f"{operation} 引用未知节点：{node_id}")
            continue
        if operation == SUPERSEDE:
            successor = payload.get("superseded_by")
            if successor not in states:
                problems.append(f"supersede 指向未知节点：{node_id} -> {successor}")
            if state["status"] != NodeStatus.ACTIVE.value:
                problems.append(f"节点 {node_id} 已是 {state['status']} 仍被作废")
            state.update(
                status=NodeStatus.SUPERSEDED.value,
                superseded_by=successor,
                note=payload.get("reason", ""),
            )
        elif operation == EXPIRE:
            if state["status"] != NodeStatus.ACTIVE.value:
                problems.append(f"节点 {node_id} 已是 {state['status']} 仍被标记过期")
            state.update(status=NodeStatus.EXPIRED.value, note=payload.get("reason", ""))
        elif operation == COMPLETE_SUBTASK:
            if state["status"] != NodeStatus.ACTIVE.value:
                problems.append(f"节点 {node_id} 已是 {state['status']} 仍被标记完成")
            state.update(status=NodeStatus.DONE.value)
    return states, problems
