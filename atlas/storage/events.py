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
