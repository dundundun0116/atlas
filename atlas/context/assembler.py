"""四区上下文组装器（决策清单 #4）+ 规则式结构查账（决策清单 #3）。

四区布局（顺序固定，前缀稳定友好）：
  ① 当前计划与约束 —— 从图现取，每步重注入，绝不裁剪（查账依赖它）
  ② 已完成工作摘要 —— 历次结转产物，超预算时最旧的先裁
  ③ 近期原始记录   —— 上次结转以来的原始条目，超预算时最旧的先裁
  ④ 踩坑与提示     —— engine.hints()，超预算时从头部裁（最旧的先去）

动态预算 v1（可解释规则）：
  计划区全额保留；剩余预算若装得下三区的自然需求则全给（需求优先）；
  装不下时按份额 summary/recent/hints = 0.30/0.55/0.15 分配，但每区不低于
  floors（可配置），然后区内裁剪。

查账：组装结果中必须出现每个活跃 Constraint/Decision 的 [ID] 标记，
缺失即抛 AuditError（决策清单 #3：活跃约束/决定缺席即报错）。
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence, Tuple

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType

ZONE_PLAN = "## 当前计划与约束"
ZONE_SUMMARY = "## 已完成工作摘要"
ZONE_RECENT = "## 近期原始记录"
ZONE_HINTS = "## 踩坑与提示"

DEFAULT_SHARES = {"summary": 0.30, "recent": 0.55, "hints": 0.15}
DEFAULT_FLOORS = {"summary": 300, "recent": 400, "hints": 120}


class PlanZoneOverflow(RuntimeError):
    """计划区自身超过总预算——查账无法保证，拒绝组装。"""


class AuditError(RuntimeError):
    """组装结果缺少活跃约束/决定的标记——查账失败。"""


@dataclass
class AssembleResult:
    text: str
    zone_tokens: Dict[str, int] = field(default_factory=dict)
    audit_ok: bool = True
    trimmed: Dict[str, bool] = field(default_factory=dict)


class ContextAssembler:
    def __init__(
        self,
        engine: GraphMemoryEngine,
        token_counter: Callable[[str], int],
        shares: Dict[str, float] = None,
        floors: Dict[str, int] = None,
    ):
        self.engine = engine
        self.count = token_counter
        self.shares = {**DEFAULT_SHARES, **(shares or {})}
        self.floors = {**DEFAULT_FLOORS, **(floors or {})}

    # ---------- 对外入口 ----------

    def assemble(self, summaries: Sequence[str], recent_raw: Sequence[str],
                 budget_tokens: int) -> AssembleResult:
        plan_text = self._render_plan()
        plan_tokens = self.count(plan_text)
        if plan_tokens + sum(self.floors.values()) > budget_tokens:
            raise PlanZoneOverflow(
                f"计划区需 {plan_tokens} tokens，加各区地板 {sum(self.floors.values())} "
                f"超过总预算 {budget_tokens}"
            )

        remaining = budget_tokens - plan_tokens
        item_lists = {
            "summary": list(summaries),
            "recent": list(recent_raw),
            "hints": self.engine.hints(),
        }
        demand = {
            key: self.count(self._render_zone(header, item_lists[key]))
            for key, header in self._ZONE_HEADERS.items()
        }
        if sum(demand.values()) <= remaining:
            allowances = demand
        else:
            allowances = {
                key: max(self.floors[key], int(self.shares[key] * remaining))
                for key in item_lists
            }

        zone_texts: Dict[str, str] = {}
        trimmed: Dict[str, bool] = {}
        for key, header in self._ZONE_HEADERS.items():
            kept, was_trimmed = self._fit_items(item_lists[key], allowances[key])
            zone_texts[key] = self._render_zone(header, kept)
            trimmed[key] = was_trimmed

        text = "\n\n".join([plan_text] + [zone_texts[k] for k in self._ZONE_HEADERS])
        text = text.rstrip() + "\n"
        self._audit(text)
        return AssembleResult(
            text=text,
            zone_tokens={"plan": plan_tokens,
                         **{k: self.count(v) for k, v in zone_texts.items()}},
            audit_ok=True,
            trimmed=trimmed,
        )

    _ZONE_HEADERS = {
        "summary": ZONE_SUMMARY,
        "recent": ZONE_RECENT,
        "hints": ZONE_HINTS,
    }

    # ---------- 区渲染 ----------

    def render_plan(self) -> str:
        """计划区单独渲染（组装失败降级时也要保证状态可见）。"""
        return self._render_plan()

    def _render_plan(self) -> str:
        eng = self.engine
        lines = [ZONE_PLAN]
        goal = eng.current_goal()
        if goal:
            lines.append(f"目标: [{goal.id}] {goal.content}")
        open_ids = eng.dependency_order([s.id for s in eng.open_subtasks()])
        if open_ids:
            lines.append("子任务（依赖序）:")
            for sid in open_ids:
                deps = [e.dst for e in eng.edges
                        if e.type == EdgeType.DEPENDS_ON and e.src == sid]
                dep_note = f" (依赖 {', '.join(deps)})" if deps else ""
                lines.append(f"  - [{sid}] {eng.nodes[sid].content}{dep_note}")
        cons = eng.active_constraints()
        if cons:
            lines.append("活跃约束:")
            lines += [f"  - [{c.id}] {c.content}" for c in cons]
        decs = eng.active_decisions()
        if decs:
            lines.append("关键决定:")
            lines += [f"  - [{d.id}] {d.content} (原因: {d.reason})" for d in decs]
        facts = [n for n in eng.nodes.values()
                 if n.type == NodeType.FACT and n.status == NodeStatus.ACTIVE]
        if facts:
            lines.append("环境事实:")
            lines += [f"  - [{f.id}] {f.content}" for f in facts]
        artifacts = [n for n in eng.nodes.values()
                     if n.type == NodeType.ARTIFACT and n.status == NodeStatus.ACTIVE]
        if artifacts:
            lines.append("产出物:")
            lines += [f"  - [{a.id}] {a.content}" for a in artifacts]
        return "\n".join(lines)

    @staticmethod
    def _render_zone(header: str, items: List[str]) -> str:
        body = "\n".join(items) if items else "（无）"
        return f"{header}\n{body}"

    def _fit_items(self, items: List[str], allowance: int) -> Tuple[List[str], bool]:
        """区内裁剪：头部（最旧）先去，保留尾部，直到区 token 数 <= allowance。"""
        kept = list(items)

        def cost(seq: List[str]) -> int:
            return self.count("\n".join(seq)) if seq else 0

        trimmed = False
        while kept and cost(kept) > allowance:
            kept.pop(0)
            trimmed = True
        return kept, trimmed

    # ---------- 查账（决策清单 #3） ----------

    def _audit(self, text: str) -> None:
        must_show = self.engine.active_constraints() + self.engine.active_decisions()
        missing = [n.id for n in must_show if f"[{n.id}]" not in text]
        if missing:
            raise AuditError(f"活跃约束/决定未出现在组装结果中: {missing}")
