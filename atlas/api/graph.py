"""Atlas API 侧的图适配器：把 Orchestrator 接到 repository（不另建内存真源）。

Orchestrator 只要求 `state_view(role)` / `role_note(...)`；这里全部转发到
`ServiceRuntime`（进而落 repository），并补上 `known_ids()` 供审计引用校验，
以及 `artifact_revise` 的作废语义（旧稿 superseded，保留原因）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from atlas.service import ServiceRuntime


class RepositoryGraph:
    def __init__(self, repository, session_id: str):
        self.repository = repository
        self.session_id = session_id
        self.service = ServiceRuntime(repository, session_id)

    def known_ids(self) -> Set[str]:
        return {node.node_id for node in self.repository.nodes(self.session_id)}

    def state_view(self, role: str) -> str:
        nodes = [n for n in self.repository.nodes(self.session_id) if n.status == "active"]
        lines = [f"[{role}] 活跃节点 {len(nodes)} 个"]
        lines += [f"- {n.type}: {str(n.content)[:60]}" for n in nodes[-5:]]
        return "\n".join(lines)

    def role_note(
        self,
        role: str,
        kind: str,
        content: str,
        reason: str = "",
        evidence: str = "",
        source: str = "",
        confidence: float = 0.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if kind == "artifact_revise":
            drafts = [n for n in self.repository.nodes(self.session_id)
                      if n.type == "artifact" and n.actor == role and n.status == "active"]
            row = self.service.note(role, content, kind="artifact", reason=reason,
                                    evidence=evidence, source=source,
                                    confidence=confidence, meta=meta)
            if drafts:
                self.service.supersede(drafts[-1].node_id, row.node_id,
                                       reason=reason or "auditor 打回后修订")
            return {"node_id": row.node_id, "kind": kind, "actor": role}
        row = self.service.note(role, content, kind=kind, reason=reason,
                                evidence=evidence, source=source,
                                confidence=confidence, meta=meta)
        return {"node_id": row.node_id, "kind": kind, "actor": role}
