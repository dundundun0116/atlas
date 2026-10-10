"""LLM 结转提取器：把原始记录段结构化写入图（决策 #11–#13 的落地）。

分工（决策 #12：规则+LLM 混合）：
- 规则负责确定性动作：子任务注册、完成标记、判分记录（见 atlas/service.py）；
- LLM 只做语义状态：goal/fact/constraint/decision/artifact 节点、依赖边、作废更新。

协议：`extract(recent_raw, engine, next_question=None) -> ExtractionResult`；
LLM 以 `Callable[[str], str]` 注入（测试用替身，生产为 openai 客户端）。
单个操作不合法则跳过并记录；整段解析失败则降级（不写图，error 非空），
调用方以规则摘要兜底（CarryOverManager）。写入的节点 actor="extractor"（多写者预留）。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType

# LLM 允许新建的节点类型；Subtask 由规则注册（决策 #12），Goal 只允许建一次
_LLM_NODE_TYPES: Dict[str, NodeType] = {
    "goal": NodeType.GOAL,
    "fact": NodeType.FACT,
    "constraint": NodeType.CONSTRAINT,
    "decision": NodeType.DECISION,
    "artifact": NodeType.ARTIFACT,
}

_LLM_EDGE_TYPES: Dict[str, EdgeType] = {
    "depends_on": EdgeType.DEPENDS_ON,
    "constrains": EdgeType.CONSTRAINS,
    "decided_by": EdgeType.DECIDED_BY,
    "subtask_of": EdgeType.SUBTASK_OF,
}

_ADDER_METHOD = {
    NodeType.GOAL: "add_goal",
    NodeType.FACT: "add_fact",
    NodeType.CONSTRAINT: "add_constraint",
    NodeType.DECISION: "add_decision",
    NodeType.ARTIFACT: "add_artifact",
}

_LLM_ACTOR = "extractor"

# 记录段截断保护：`## Tool Calls Info` 里是整段工具轨迹（含完整推理文本），动辄上万字符，
# 且对状态提取是噪声；Task/solution/Judge 都在前部，截断天然保留有效信息、控制提示词规模。
_RAW_ENTRY_CAP = 3500
_RAW_TOTAL_CAP = 7000

_SYSTEM_PROMPT = """You maintain a structured execution-state graph for a long-horizon math reasoning task.

Given the current graph state, the latest raw record segment, and (optionally) the upcoming question,
extract durable execution state and write a short narrative summary.

Output strict JSON only:
{"summary": "<2-4 sentences on what this segment did; narrative only, no state details>",
 "operations": [...]}

Allowed operations:
1) {"op": "add_node", "type": "goal|fact|constraint|decision|artifact", "content": "...",
    "note": "", "reason": "...", "evidence": "...",
    "constrains": "<existing id>", "decided_by": "<existing id>"}
   - goal: only if the graph has no goal node yet (the overall objective of this problem series).
   - fact: definitions, lemmas, paper-given results. constraint: assumptions/conditions the task
     must respect (e.g. "gamma*L <= 1/24", "each f_i is L-smooth").
   - decision: a method choice made while solving, with its reason. artifact: a derived result produced.
   - reason is required for decision; constrains/decided_by are optional links to existing ids.
2) {"op": "add_edge", "src": "<id>", "dst": "<id>", "type": "depends_on|constrains|decided_by|subtask_of"}
   - depends_on: use when the new question clearly builds on an earlier subtask's result.
3) {"op": "supersede", "target": "<existing id>", "content": "...", "reason": "..."}
   - use when the new record corrects or contradicts an existing node; never silently drop information.

Rules:
- Keep all LaTeX/math verbatim; do not paraphrase formulas.
- Reference only ids that exist in the graph state; never invent ids.
- Do not create a node that duplicates an existing active node; reference the existing id instead.
- If a record was judged INCORRECT, do not record its answer as an artifact; only durable lessons.
- Skip noise; if nothing durable, return empty operations.
- Output JSON only, no commentary."""


class _OpError(Exception):
    """单个操作不合法（跳过并记录，不影响同批其他操作）。"""


@dataclass
class ExtractionResult:
    ops_applied: List[str] = field(default_factory=list)
    ops_skipped: List[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    raw_output: str = ""


class LLMCarryOverExtractor:
    def __init__(self, llm: Callable[[str], str]):
        self.llm = llm

    def __call__(self, recent_raw: Sequence[str], engine: GraphMemoryEngine) -> ExtractionResult:
        return self.extract(recent_raw, engine)

    def extract(
        self,
        recent_raw: Sequence[str],
        engine: GraphMemoryEngine,
        next_question: Optional[str] = None,
    ) -> ExtractionResult:
        prompt = self._build_prompt(engine, recent_raw, next_question)
        try:
            raw = self.llm(prompt) or ""
        except Exception as e:  # LLM 通道故障 → 降级，不打断跑分
            return ExtractionResult(error=f"LLM 调用失败：{type(e).__name__}: {e}")
        payload = _parse_json(raw)
        if payload is None:
            excerpt = " ".join(raw.split())[:200]
            return ExtractionResult(
                error=f"LLM 输出无法解析为 JSON（前 200 字符：{excerpt!r}）", raw_output=raw
            )
        ops = payload.get("operations") or []
        if not isinstance(ops, list):
            return ExtractionResult(error="operations 不是列表", raw_output=raw)
        applied, skipped = self._apply(engine, ops)
        summary = str(payload.get("summary") or "").strip()
        return ExtractionResult(
            ops_applied=applied, ops_skipped=skipped, summary=summary, raw_output=raw
        )

    # ---------- 提示词 ----------

    def _build_prompt(
        self, engine: GraphMemoryEngine, recent_raw: Sequence[str], next_question: Optional[str]
    ) -> str:
        parts = [_SYSTEM_PROMPT, "", "[GRAPH STATE]", _render_graph(engine),
                 "", "[RAW RECORD SEGMENT]", _render_raw(recent_raw)]
        if next_question:
            parts += ["", "[UPCOMING QUESTION]", next_question]
        return "\n".join(parts)

    # ---------- 操作应用 ----------

    def _apply(self, engine: GraphMemoryEngine, ops: List) -> Tuple[List[str], List[str]]:
        applied: List[str] = []
        skipped: List[str] = []
        for i, op in enumerate(ops):
            label = f"ops[{i}]"
            if not isinstance(op, dict):
                skipped.append(f"{label}: 不是对象")
                continue
            kind = op.get("op")
            try:
                if kind == "add_node":
                    desc, warnings = self._apply_add_node(engine, op)
                    skipped.extend(f"{label}: {w}" for w in warnings)
                elif kind == "add_edge":
                    desc = self._apply_add_edge(engine, op)
                elif kind == "supersede":
                    desc = self._apply_supersede(engine, op)
                else:
                    raise _OpError(f"未知操作 {kind!r}")
            except _OpError as e:
                skipped.append(f"{label}: {e}")
            except Exception as e:  # 引擎校验兜底（非法边组合等）
                skipped.append(f"{label}: {type(e).__name__}: {e}")
            else:
                applied.append(desc)
        return applied, skipped

    def _apply_add_node(self, engine: GraphMemoryEngine, op: Dict) -> Tuple[str, List[str]]:
        type_name = str(op.get("type") or "")
        node_type = _LLM_NODE_TYPES.get(type_name)
        if node_type is None:
            raise _OpError(f"不允许的节点类型 {type_name!r}")
        content = str(op.get("content") or "").strip()
        if not content:
            raise _OpError("content 为空")
        if node_type == NodeType.GOAL and engine.current_goal() is not None:
            raise _OpError("已有 goal 节点")
        if _is_duplicate(engine, node_type, content):
            raise _OpError(f"与既有活跃节点重复：{content[:40]}")
        kwargs = {"note": str(op.get("note") or ""), "actor": _LLM_ACTOR}
        if node_type == NodeType.DECISION:
            reason = str(op.get("reason") or "").strip()
            if not reason:
                raise _OpError("decision 缺 reason")
            kwargs["reason"] = reason
            kwargs["evidence"] = str(op.get("evidence") or "")
        node = getattr(engine, _ADDER_METHOD[node_type])(content, **kwargs)

        warnings: List[str] = []
        for field_name, edge_type in (("constrains", EdgeType.CONSTRAINS),
                                      ("decided_by", EdgeType.DECIDED_BY)):
            target = str(op.get(field_name) or "").strip()
            if not target:
                continue
            try:
                engine.link(node.id, target, edge_type)
            except Exception as e:
                warnings.append(f"{node.id} 的 {field_name}={target} 未建立（{type(e).__name__}）")
        return f"add {node.id} {node_type.value}: {content[:60]}", warnings

    def _apply_add_edge(self, engine: GraphMemoryEngine, op: Dict) -> str:
        src, dst = str(op.get("src") or ""), str(op.get("dst") or "")
        edge_type = _LLM_EDGE_TYPES.get(str(op.get("type") or ""))
        if edge_type is None:
            raise _OpError("不允许的边类型")
        if src not in engine.nodes or dst not in engine.nodes:
            raise _OpError(f"引用了不存在的节点 {src}->{dst}")
        if src == dst:
            raise _OpError("自环")
        if edge_type == EdgeType.DEPENDS_ON and _depends_transitively(engine, dst, src):
            raise _OpError(f"{dst} 已（间接）依赖 {src}，反向会成环")
        engine.link(src, dst, edge_type)
        return f"add edge {src} -{edge_type.value}-> {dst}"

    def _apply_supersede(self, engine: GraphMemoryEngine, op: Dict) -> str:
        target = str(op.get("target") or "")
        if target not in engine.nodes:
            raise _OpError(f"target {target} 不存在")
        content = str(op.get("content") or "").strip()
        reason = str(op.get("reason") or "").strip()
        if not content or not reason:
            raise _OpError("supersede 需要 content 与 reason")
        node = engine.supersede_with(target, new_content=content, reason=reason, actor=_LLM_ACTOR)
        return f"supersede {target} -> {node.id}: {reason[:60]}"


# ---------- 工具 ----------


def _render_raw(recent_raw: Sequence[str]) -> str:
    parts = []
    for entry in recent_raw:
        text = entry.strip()
        if len(text) > _RAW_ENTRY_CAP:
            text = text[:_RAW_ENTRY_CAP].rstrip() + "\n…（本条过长，已截断）"
        parts.append(text)
    joined = "\n\n".join(parts)
    if len(joined) > _RAW_TOTAL_CAP:
        joined = joined[:_RAW_TOTAL_CAP].rstrip() + "\n…（记录段过长，已截断）"
    return joined


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_duplicate(engine: GraphMemoryEngine, node_type: NodeType, content: str) -> bool:
    """同类型活跃节点已存在同样内容（防 LLM 跨段重复写，导致图膨胀）。"""
    return any(
        n.type == node_type and n.status == NodeStatus.ACTIVE and n.content.strip() == content
        for n in engine.nodes.values()
    )


def _depends_transitively(engine: GraphMemoryEngine, start: str, target: str) -> bool:
    """start 是否（沿 DEPENDS_ON 边）间接依赖 target。"""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack += [
            e.dst for e in engine.edges
            if e.type == EdgeType.DEPENDS_ON and e.src == cur
        ]
    return False


def _render_graph(engine: GraphMemoryEngine) -> str:
    if not engine.nodes:
        return "（空图）"
    lines = []
    for node in engine.nodes.values():
        extra = []
        if node.note:
            extra.append(f"note: {node.note}")
        if node.reason:
            extra.append(f"reason: {node.reason}")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(
            f"- [{node.id}] ({node.type.value}, {node.status.value}) {node.content}{suffix}"
        )
    return "\n".join(lines)
