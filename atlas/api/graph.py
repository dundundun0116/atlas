"""Atlas API 侧的图适配器：把 Orchestrator 接到 repository（不另建内存真源）。

Orchestrator 只要求 `state_view(role)` / `role_note(...)`；这里全部转发到
`ServiceRuntime`（进而落 repository），并补上 `known_ids()` 供审计引用校验，
以及 `artifact_revise` 的作废语义（旧稿 superseded，保留原因）。

**上下文主路径**：`state_view` 不再返回"最近 5 个节点"摘要，而是走四区组装器
（`atlas.context.builder`），并把这次的 ContextPackage（digest / 分区 token /
包含与排除的引用）落库。模型看到的上下文与审计看到的是同一份。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, Set

from atlas.context.builder import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_ZONES,
    ContextPackage,
)
from atlas.service import ServiceRuntime


class RepositoryGraph:
    def __init__(
        self,
        repository,
        session_id: str,
        *,
        context_budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        context_token_counter: Optional[Callable[[str], int]] = None,
        context_zones: Sequence[str] = DEFAULT_ZONES,
    ):
        self.repository = repository
        self.session_id = session_id
        self.service = ServiceRuntime(repository, session_id)
        self.context_budget_tokens = int(context_budget_tokens)
        self.context_token_counter = context_token_counter
        self.context_zones = tuple(context_zones)
        #: 最近一次组装出的上下文包（调用方可据此写 digest、做下钻）。
        self.last_context: Optional[ContextPackage] = None

    def known_ids(self) -> Set[str]:
        return {node.node_id for node in self.repository.nodes(self.session_id)}

    def context_package(self, role: str, focus: str = "") -> ContextPackage:
        """组装 + 落库一次上下文包；同一角色的调用各自留一条记录。"""
        package = self.service.build_context(
            role,
            budget_tokens=self.context_budget_tokens,
            focus=focus,
            token_counter=self.context_token_counter,
            zones=self.context_zones,
        )
        self.last_context = package
        return package

    def state_view(self, role: str) -> str:
        """角色看到的状态视图 = 四区组装结果（预算内、带来源与风险段）。"""
        return self.context_package(role).text

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
