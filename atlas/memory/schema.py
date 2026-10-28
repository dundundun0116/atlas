"""图状态记忆引擎的 schema：六类节点、五类边、status 生命周期。

对应 PROJECT.md 决策清单 #1：
- 作废机制 = 时效字段+决策变迁（active→superseded/expired，superseded_by 指针），
  不建执行树；
- Decision 节点带 reason/evidence；行动者字段 actor 预留多写者。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class NodeType(str, Enum):
    GOAL = "goal"
    SUBTASK = "subtask"
    CONSTRAINT = "constraint"
    DECISION = "decision"
    ARTIFACT = "artifact"
    FACT = "fact"

    @property
    def id_prefix(self) -> str:
        return {
            NodeType.GOAL: "G",
            NodeType.SUBTASK: "S",
            NodeType.CONSTRAINT: "C",
            NodeType.DECISION: "D",
            NodeType.ARTIFACT: "A",
            NodeType.FACT: "F",
        }[self]


class NodeStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"            # 仅 Subtask/Goal：完成（区别于作废）
    SUPERSEDED = "superseded"  # 被接替（作废的一种，留有接替指针）
    EXPIRED = "expired"        # 时效过期（作废的一种）


class EdgeType(str, Enum):
    SUBTASK_OF = "subtask_of"
    DEPENDS_ON = "depends_on"
    CONSTRAINS = "constrains"
    DECIDED_BY = "decided_by"
    SUPERSEDES = "supersedes"


# 合法的 (边类型, 源节点类型, 目标节点类型) 组合；SUPERSEDES 单独校验（同类型）
LEGAL_EDGES: set[tuple[EdgeType, NodeType, NodeType]] = {
    (EdgeType.SUBTASK_OF, NodeType.SUBTASK, NodeType.GOAL),
    (EdgeType.DEPENDS_ON, NodeType.SUBTASK, NodeType.SUBTASK),
    (EdgeType.CONSTRAINS, NodeType.CONSTRAINT, NodeType.SUBTASK),
    (EdgeType.CONSTRAINS, NodeType.CONSTRAINT, NodeType.GOAL),
    (EdgeType.CONSTRAINS, NodeType.CONSTRAINT, NodeType.DECISION),
    (EdgeType.DECIDED_BY, NodeType.ARTIFACT, NodeType.DECISION),
    (EdgeType.DECIDED_BY, NodeType.FACT, NodeType.DECISION),
    (EdgeType.SUPERSEDES, NodeType.DECISION, NodeType.DECISION),
    (EdgeType.SUPERSEDES, NodeType.CONSTRAINT, NodeType.CONSTRAINT),
    (EdgeType.SUPERSEDES, NodeType.FACT, NodeType.FACT),
    (EdgeType.SUPERSEDES, NodeType.SUBTASK, NodeType.SUBTASK),
    # 编排修订：auditor 打回后 worker 的修订稿作废草稿——否则错误草稿
    # 会作为活跃产出物留在图里（矛盾共存），与项目要防的失败模式直接冲突。
    (EdgeType.SUPERSEDES, NodeType.ARTIFACT, NodeType.ARTIFACT),
}


@dataclass
class Node:
    type: NodeType
    content: str
    id: str = ""
    actor: str = "main"
    created_at: str = ""
    status: NodeStatus = NodeStatus.ACTIVE
    superseded_by: Optional[str] = None
    note: str = ""       # 作废/过期原因或补充标注
    reason: str = ""     # Decision 专属：为什么做这个决定
    evidence: str = ""   # Decision 专属：依据
    source: str = ""        # 阶段 D：来源（论文/chunk/角色/工具）
    confidence: float = 0.0 # 阶段 D：0–1 可信度
    valid_from: str = ""    # 阶段 D：生效起点（ISO）
    valid_until: str = ""   # 阶段 D：失效时点（ISO），空=不过期
    verified_at: str = ""   # 阶段 D：最后一次核验时间（ISO）
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "actor": self.actor,
            "created_at": self.created_at,
            "status": self.status.value,
            "superseded_by": self.superseded_by,
            "note": self.note,
            "reason": self.reason,
            "evidence": self.evidence,
            "source": self.source,
            "confidence": float(self.confidence or 0.0),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "verified_at": self.verified_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(
            id=d["id"],
            type=NodeType(d["type"]),
            content=d["content"],
            actor=d.get("actor", "main"),
            created_at=d.get("created_at", ""),
            status=NodeStatus(d.get("status", NodeStatus.ACTIVE.value)),
            superseded_by=d.get("superseded_by"),
            note=d.get("note", ""),
            reason=d.get("reason", ""),
            evidence=d.get("evidence", ""),
            source=d.get("source", ""),
            confidence=float(d.get("confidence") or 0.0),
            valid_from=d.get("valid_from", ""),
            valid_until=d.get("valid_until", ""),
            verified_at=d.get("verified_at", ""),
            meta=d.get("meta", {}),
        )


@dataclass
class Edge:
    src: str
    dst: str
    type: EdgeType
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "edge",
            "src": self.src,
            "dst": self.dst,
            "type": self.type.value,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Edge":
        return cls(
            src=d["src"],
            dst=d["dst"],
            type=EdgeType(d["type"]),
            created_at=d.get("created_at", ""),
        )
