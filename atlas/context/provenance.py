"""状态来源与有效期（阶段 D）：让每条状态能回答"谁给的、多可信、何时失效"。

组装前的治理三问：
1. 过期的约束/决策/事实还能不能进计划区？—— 不能，但要留下"为什么被挡"；
2. 两条互斥约束同时活跃？—— 显式进风险区，不静默挑一条；
3. 产出物验证过没有？—— 未验证的 Artifact 不进"有效产出物"，进风险区。

所有判断都是纯函数、只读节点，保证同一图快照 + 同一预算下组装结果可复现。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from atlas.memory.schema import Node, NodeStatus, NodeType

#: 一条状态应该能说清的来源信息。
PROVENANCE_FIELDS = (
    "source",
    "actor",
    "confidence",
    "valid_from",
    "valid_until",
    "verified_at",
)

#: 需要 provenance 的节点类型（Goal/Subtask 是过程节点，不强制）。
PROVENANCE_REQUIRED_TYPES = (
    NodeType.FACT,
    NodeType.CONSTRAINT,
    NodeType.DECISION,
    NodeType.ARTIFACT,
)

_EXCLUSIVE_KEY_FIELDS = ("key", "topic", "subject")


def _parse_time(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def is_stale(node: Node, now: Optional[datetime] = None) -> bool:
    """`valid_until` 已过 → 过期（未设则永不过期）。"""
    limit = _parse_time(getattr(node, "valid_until", ""))
    if limit is None:
        return False
    return (now or datetime.now()) >= limit


def stale_reason(node: Node, now: Optional[datetime] = None) -> str:
    if not is_stale(node, now):
        return ""
    return f"valid_until={node.valid_until} 已过期"


def is_unverified(node: Node) -> bool:
    """产出物没标 verified_at 就算未验证（Fact 本身语义即"已核验事实"，不在此列）。"""
    return node.type is NodeType.ARTIFACT and not str(getattr(node, "verified_at", "") or "").strip()


def missing_provenance(node: Node) -> bool:
    return node.type in PROVENANCE_REQUIRED_TYPES and not str(
        getattr(node, "source", "") or ""
    ).strip()


def constraint_key(node: Node) -> str:
    """同一 key/topic/subject 的约束视为"同一件事的不同版本"，才可能互斥。"""
    for name in _EXCLUSIVE_KEY_FIELDS:
        value = str((node.meta or {}).get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass
class ConstraintConflict:
    key: str
    node_ids: List[str]
    contents: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "node_ids": list(self.node_ids), "contents": list(self.contents)}


@dataclass
class ProvenanceAudit:
    """一次组装的治理报告：被挡的节点、冲突、未验证产出、缺来源。"""

    stale: List[Dict[str, str]] = field(default_factory=list)
    conflicts: List[ConstraintConflict] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)
    missing_source: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.stale or self.conflicts or self.unverified or self.missing_source)

    def filtered_ids(self) -> Dict[str, str]:
        """节点 id → 被挡原因（供组装统计）。"""
        return {item["node_id"]: item["reason"] for item in self.stale}

    def warnings(self) -> List[str]:
        lines: List[str] = []
        for item in self.stale:
            lines.append(f"[{item['node_id']}] 已过期（{item['reason']}），未进入有效计划区")
        for conflict in self.conflicts:
            ids = "、".join(conflict.node_ids)
            lines.append(f"约束冲突（key={conflict.key}）：{ids} 内容互斥，需人工确认")
        for node_id in self.unverified:
            lines.append(f"[{node_id}] 产出物未验证（缺 verified_at）")
        for node_id in self.missing_source:
            lines.append(f"[{node_id}] 缺少来源（source）")
        return lines

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stale": list(self.stale),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "unverified": list(self.unverified),
            "missing_source": list(self.missing_source),
        }


def audit_nodes(nodes: Iterable[Node], now: Optional[datetime] = None) -> ProvenanceAudit:
    """只读审计：给定图快照，产出治理报告（顺序稳定：按节点 id 排序）。"""
    ordered = sorted(nodes, key=lambda n: n.id)
    audit = ProvenanceAudit()
    active = [n for n in ordered if n.status is NodeStatus.ACTIVE]
    for node in active:
        if is_stale(node, now):
            audit.stale.append({"node_id": node.id, "reason": stale_reason(node, now)})
        if is_unverified(node):
            audit.unverified.append(node.id)
        if missing_provenance(node):
            audit.missing_source.append(node.id)

    groups: Dict[str, List[Node]] = {}
    for node in active:
        if node.type is not NodeType.CONSTRAINT:
            continue
        key = constraint_key(node)
        if key:
            groups.setdefault(key, []).append(node)
    for key in sorted(groups):
        contents = {n.content.strip() for n in groups[key]}
        if len(groups[key]) > 1 and len(contents) > 1:
            audit.conflicts.append(
                ConstraintConflict(
                    key=key,
                    node_ids=[n.id for n in groups[key]],
                    contents=[n.content for n in groups[key]],
                )
            )
    return audit


def risk_lines(audit: ProvenanceAudit) -> List[str]:
    """把审计结果渲染成计划区末尾的风险段（空审计不产生任何行）。"""
    warnings = audit.warnings()
    if not warnings:
        return []
    return ["风险与待确认:"] + [f"  - {line}" for line in warnings]
