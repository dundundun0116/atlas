"""图状态记忆引擎：写入、作废、查询、持久化。

读接口只返回 active 节点（决策清单 #1）；dependency_order 保证子任务按依赖序
输出；hints() 合成"踩坑提示"（H 区数据源：被推翻的决定+原因、已过期约束）。
持久化为 JSONL（跨会话，MemoryArena 多会话任务需要）。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

from .schema import (
    LEGAL_EDGES,
    Edge,
    EdgeType,
    Node,
    NodeStatus,
    NodeType,
)

Ref = Union[str, Node]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class GraphMemoryEngine:
    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self._counters: Dict[str, int] = {}

    # ---------- 写入 ----------

    def _new_node(self, node: Node) -> Node:
        prefix = node.type.id_prefix
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        node.id = f"{prefix}{self._counters[prefix]}"
        node.created_at = node.created_at or _now()
        self.nodes[node.id] = node
        return node

    def _add(self, type_: NodeType, content: str, **kwargs) -> Node:
        if not str(content or "").strip():
            raise ValueError(f"{type_.value} content 不能为空")
        return self._new_node(Node(type=type_, content=content, **kwargs))

    def add_goal(self, content: str, **kwargs) -> Node:
        return self._add(NodeType.GOAL, content, **kwargs)

    def add_subtask(self, content: str, **kwargs) -> Node:
        return self._add(NodeType.SUBTASK, content, **kwargs)

    def add_constraint(self, content: str, **kwargs) -> Node:
        return self._add(NodeType.CONSTRAINT, content, **kwargs)

    def add_artifact(self, content: str, **kwargs) -> Node:
        return self._add(NodeType.ARTIFACT, content, **kwargs)

    def add_fact(self, content: str, **kwargs) -> Node:
        return self._add(NodeType.FACT, content, **kwargs)

    def add_decision(self, content: str, reason: str = "", evidence: str = "", **kwargs) -> Node:
        if not str(reason or "").strip():
            raise ValueError("Decision 节点必须带 reason（决策变迁的依据）")
        return self._add(
            NodeType.DECISION, content, reason=reason, evidence=evidence, **kwargs
        )

    def link(self, src: Ref, dst: Ref, type_: EdgeType) -> Edge:
        src_id, dst_id = self._ref(src), self._ref(dst)
        combo = (type_, self.nodes[src_id].type, self.nodes[dst_id].type)
        if combo not in LEGAL_EDGES:
            raise ValueError(f"非法边组合：{combo[1].value} --{type_.value}--> {combo[2].value}")
        edge = Edge(src=src_id, dst=dst_id, type=type_, created_at=_now())
        self.edges.append(edge)
        return edge

    # ---------- 作废 / 生命周期 ----------

    def supersede(self, old_id: Ref, by_id: Ref) -> None:
        """已有节点接替旧节点：旧节点置 superseded 并建 supersedes 边。"""
        old, new = self._ref(old_id), self._ref(by_id)
        if old == new:
            raise ValueError("节点不能接替自己")
        if self.nodes[old].status != NodeStatus.ACTIVE:
            raise ValueError(f"{old} 状态为 {self.nodes[old].status.value}，不能再次作废")
        if self.nodes[new].status != NodeStatus.ACTIVE:
            raise ValueError(f"接替者 {new} 必须是 active 节点")
        self.nodes[old].status = NodeStatus.SUPERSEDED
        self.nodes[old].superseded_by = new
        self.link(new, old, EdgeType.SUPERSEDES)

    def supersede_with(
        self,
        old_id: Ref,
        new_content: str,
        reason: str,
        evidence: str = "",
        actor: str = "main",
    ) -> Node:
        """创建同类型的接替节点并完成作废。reason 记录推翻原因（决策变迁）。"""
        old = self.nodes[self._ref(old_id)]
        if not str(reason or "").strip():
            raise ValueError("作废必须记录原因")
        kwargs = {"actor": actor}
        if old.type == NodeType.DECISION:
            new = self.add_decision(new_content, reason=reason, evidence=evidence, **kwargs)
        else:
            new = self._add(old.type, new_content, **kwargs)
            new.note = reason
        self.supersede(old.id, new.id)
        return new

    def expire(self, node_id: Ref, reason: str) -> None:
        node = self.nodes[self._ref(node_id)]
        if node.status != NodeStatus.ACTIVE:
            raise ValueError(f"{node.id} 状态为 {node.status.value}，不能过期")
        if not str(reason or "").strip():
            raise ValueError("过期必须记录原因")
        node.status = NodeStatus.EXPIRED
        node.note = reason

    def complete_subtask(self, node_id: Ref) -> None:
        node = self.nodes[self._ref(node_id)]
        if node.type != NodeType.SUBTASK:
            raise ValueError(f"{node.id} 不是 Subtask")
        if node.status != NodeStatus.ACTIVE:
            raise ValueError(f"{node.id} 状态为 {node.status.value}，不能标记完成")
        node.status = NodeStatus.DONE

    # ---------- 查询（只取 active；决策清单 #1） ----------

    def current_goal(self) -> Optional[Node]:
        goals = [n for n in self.nodes.values()
                 if n.type == NodeType.GOAL and n.status == NodeStatus.ACTIVE]
        return goals[-1] if goals else None

    def active_constraints(self) -> List[Node]:
        return self._active_of(NodeType.CONSTRAINT)

    def active_decisions(self) -> List[Node]:
        return self._active_of(NodeType.DECISION)

    def open_subtasks(self) -> List[Node]:
        return self._active_of(NodeType.SUBTASK)

    def _active_of(self, type_: NodeType) -> List[Node]:
        return [n for n in self.nodes.values()
                if n.type == type_ and n.status == NodeStatus.ACTIVE]

    def dependency_order(self, subtask_ids: Iterable[str]) -> List[str]:
        """Kahn 拓扑排序（按 DEPENDS_ON 边），依赖在前。"""
        ids = [self._ref(s) for s in subtask_ids]
        idset = set(ids)
        for sid in ids:
            if self.nodes[sid].type != NodeType.SUBTASK:
                raise ValueError(f"{sid} 不是 Subtask")
        indegree = {sid: 0 for sid in ids}
        successors: Dict[str, List[str]] = {sid: [] for sid in ids}
        for e in self.edges:
            if e.type == EdgeType.DEPENDS_ON and e.src in idset and e.dst in idset:
                indegree[e.src] += 1
                successors[e.dst].append(e.src)
        queue = sorted(sid for sid in ids if indegree[sid] == 0)
        ordered: List[str] = []
        while queue:
            sid = queue.pop(0)
            ordered.append(sid)
            for succ in successors[sid]:
                indegree[succ] -= 1
                if indegree[succ] == 0:
                    queue.append(succ)
                    queue.sort()
        if len(ordered) != len(ids):
            raise ValueError("子任务依赖存在环")
        return ordered

    def decision_history(self, node_id: Ref) -> List[Node]:
        """给定最新决定，沿 supersedes 链回溯整条决策变迁（旧→新）。"""
        chain: List[Node] = [self.nodes[self._ref(node_id)]]
        while True:
            # supersedes 边方向：new --supersedes--> old；找当前节点接替的对象
            prev = next(
                (e.dst for e in self.edges
                 if e.type == EdgeType.SUPERSEDES and e.src == chain[-1].id),
                None,
            )
            if prev is None:
                break
            chain.append(self.nodes[prev])
        return list(reversed(chain))

    def hints(self) -> List[str]:
        """踩坑提示（H 区数据源）：被推翻的节点+推翻原因、已过期节点+原因。"""
        lines: List[str] = []
        for e in self.edges:
            if e.type != EdgeType.SUPERSEDES:
                continue
            old, new = self.nodes[e.dst], self.nodes[e.src]
            why = new.reason if new.type == NodeType.DECISION else new.note
            lines.append(f"[{old.id}] {old.content} ——已作废，原因：{why}（接替：{new.id}）")
        for n in self.nodes.values():
            if n.status == NodeStatus.EXPIRED:
                lines.append(f"[{n.id}] {n.content} ——已过期，原因：{n.note}")
        return lines

    # ---------- 持久化 ----------

    def save_jsonl(self, path) -> None:
        lines = [{"kind": "node", **n.to_dict()} for n in self.nodes.values()]
        lines += [e.to_dict() for e in self.edges]
        Path(path).write_text(
            "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load_jsonl(cls, path) -> "GraphMemoryEngine":
        engine = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("kind") == "edge":
                engine.edges.append(Edge.from_dict(d))
            else:
                d.pop("kind", None)
                node = Node.from_dict(d)
                engine.nodes[node.id] = node
        engine._recount()
        return engine

    def _recount(self) -> None:
        for node in self.nodes.values():
            prefix = node.type.id_prefix
            num = int(node.id[len(prefix):])
            self._counters[prefix] = max(self._counters.get(prefix, 0), num)

    # ---------- 工具 ----------

    def _ref(self, ref: Ref) -> str:
        return ref.id if isinstance(ref, Node) else ref
