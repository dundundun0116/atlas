"""图状态快照导出（决策 #10）：把图引擎渲染成自包含的 SVG / HTML。

- 布局：按节点类型分列（goal → subtask → constraint → decision → fact → artifact），
  列内按创建顺序自上而下；边按类型着色/线型（依赖虚线、作废红色点线等）。
- 状态用填充色区分：active（绿）/ done（蓝）/ superseded（灰）/ expired（橙）。
- 零依赖（手写 SVG）、确定性输出（同图同结果，便于测试与 diff）。
"""

from html import escape
from typing import Dict, List, Tuple

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType

_COLUMN_ORDER: List[NodeType] = [
    NodeType.GOAL,
    NodeType.SUBTASK,
    NodeType.CONSTRAINT,
    NodeType.DECISION,
    NodeType.FACT,
    NodeType.ARTIFACT,
]
_COLUMN_TITLE = {
    NodeType.GOAL: "目标",
    NodeType.SUBTASK: "子任务",
    NodeType.CONSTRAINT: "约束",
    NodeType.DECISION: "决定",
    NodeType.FACT: "事实",
    NodeType.ARTIFACT: "产出物",
}

_STATUS_FILL = {
    NodeStatus.ACTIVE: "#e8f5e9",
    NodeStatus.DONE: "#e3f2fd",
    NodeStatus.SUPERSEDED: "#eceff1",
    NodeStatus.EXPIRED: "#fff3e0",
}
_STATUS_STROKE = {
    NodeStatus.ACTIVE: "#2e7d32",
    NodeStatus.DONE: "#1565c0",
    NodeStatus.SUPERSEDED: "#9e9e9e",
    NodeStatus.EXPIRED: "#ef6c00",
}
_EDGE_STYLE: Dict[EdgeType, Tuple[str, str]] = {
    EdgeType.SUBTASK_OF: ("#757575", ""),
    EdgeType.DEPENDS_ON: ("#1976d2", "6 3"),
    EdgeType.CONSTRAINS: ("#ef6c00", ""),
    EdgeType.DECIDED_BY: ("#7b1fa2", ""),
    EdgeType.SUPERSEDES: ("#c62828", "3 3"),
}

_MARGIN = 24
_COLUMN_WIDTH = 250
_NODE_WIDTH = 226
_NODE_HEIGHT = 46
_NODE_GAP = 14
_HEADER_HEIGHT = 34
_TEXT_LIMIT = 26  # 单行字符数上限（超出折行，最多两行）


def _wrap(text: str, width: int = _TEXT_LIMIT, max_lines: int = 2) -> List[str]:
    words = str(text).split()
    lines: List[str] = []
    current = ""
    consumed = 0
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
            consumed += 1
            if len(lines) == max_lines:
                break
        else:
            current = candidate
            consumed += 1
    if len(lines) < max_lines and current:
        lines.append(current)
        consumed = len(words)
    if not lines:
        lines = [""]
    if consumed < len(words):  # 有内容被截断：末行加省略号
        lines[-1] = lines[-1] + "…"
    return lines


def _layout(engine: GraphMemoryEngine) -> Dict[str, Tuple[int, int, int]]:
    """返回 {node_id: (x, y, column_index)}；同列按创建顺序（dict 插入序）排列。"""
    positions: Dict[str, Tuple[int, int, int]] = {}
    for col, node_type in enumerate(_COLUMN_ORDER):
        x = _MARGIN + col * _COLUMN_WIDTH
        y = _MARGIN + _HEADER_HEIGHT
        for node in engine.nodes.values():
            if node.type != node_type:
                continue
            positions[node.id] = (x, y, col)
            y += _NODE_HEIGHT + _NODE_GAP
    return positions


def _canvas_size(positions: Dict[str, Tuple[int, int, int]]) -> Tuple[int, int]:
    if not positions:
        return (_MARGIN * 2 + _COLUMN_WIDTH, _MARGIN * 2 + _HEADER_HEIGHT + _NODE_HEIGHT)
    max_col = max(col for _, _, col in positions.values())
    max_y = max(y for _, y, _ in positions.values())
    return (
        _MARGIN * 2 + (max_col + 1) * _COLUMN_WIDTH,
        max_y + _NODE_HEIGHT + _MARGIN,
    )


def graph_to_svg(engine: GraphMemoryEngine) -> str:
    positions = _layout(engine)
    width, height = _canvas_size(positions)
    parts: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
    ]
    for color, _dash in _EDGE_STYLE.values():
        marker = f"arrow-{color.lstrip('#')}"
        parts.append(
            f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker></defs>'
        )

    # 列标题
    for col, node_type in enumerate(_COLUMN_ORDER):
        x = _MARGIN + col * _COLUMN_WIDTH
        parts.append(
            f'<text x="{x}" y="{_MARGIN + 18}" font-size="14" font-weight="600" fill="#37474f">'
            f"{escape(_COLUMN_TITLE[node_type])}</text>"
        )

    # 边（先画，压在节点下方）
    for edge in engine.edges:
        if edge.src not in positions or edge.dst not in positions:
            continue
        sx, sy, scol = positions[edge.src]
        dx, dy, dcol = positions[edge.dst]
        color, dash = _EDGE_STYLE.get(edge.type, ("#607d8b", ""))
        if scol == dcol:
            x1, y1 = sx + _NODE_WIDTH // 2, sy + _NODE_HEIGHT
            x2, y2 = dx + _NODE_WIDTH // 2, dy
            path = f"M {x1} {y1} C {x1} {y1 + 30}, {x2} {y2 - 30}, {x2} {y2}"
        elif dcol > scol:
            x1, y1 = sx + _NODE_WIDTH, sy + _NODE_HEIGHT // 2
            x2, y2 = dx, dy + _NODE_HEIGHT // 2
            mid = (x1 + x2) / 2
            path = f"M {x1} {y1} C {mid} {y1}, {mid} {y2}, {x2} {y2}"
        else:
            x1, y1 = sx, sy + _NODE_HEIGHT // 2
            x2, y2 = dx + _NODE_WIDTH, dy + _NODE_HEIGHT // 2
            mid = (x1 + x2) / 2
            path = f"M {x1} {y1} C {mid} {y1}, {mid} {y2}, {x2} {y2}"
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"{dash_attr} '
            f'marker-end="url(#arrow-{color.lstrip("#")})"/>'
        )

    # 节点
    for node in engine.nodes.values():
        if node.id not in positions:
            continue
        x, y, _ = positions[node.id]
        fill = _STATUS_FILL.get(node.status, "#ffffff")
        stroke = _STATUS_STROKE.get(node.status, "#607d8b")
        parts.append(
            f'<g><title>{escape(f"[{node.id}] ({node.type.value}/{node.status.value}) {node.content}")}</title>'
            f'<rect x="{x}" y="{y}" width="{_NODE_WIDTH}" height="{_NODE_HEIGHT}" rx="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>'
        )
        lines = _wrap(node.content)
        parts.append(
            f'<text x="{x + 8}" y="{y + 17}" font-size="11" fill="#546e7a">'
            f"{escape(node.id)} · {escape(node.type.value)} · {escape(node.status.value)}"
            f"</text>"
        )
        for i, line in enumerate(lines):
            parts.append(
                f'<text x="{x + 8}" y="{y + 32 + i * 12}" font-size="10.5" fill="#263238">'
                f"{escape(line)}</text>"
            )
        parts.append("</g>")

    parts.append("</svg>")
    return "\n".join(parts)


def _legend_html() -> str:
    items = [
        ("active 活跃", _STATUS_FILL[NodeStatus.ACTIVE], _STATUS_STROKE[NodeStatus.ACTIVE]),
        ("done 完成", _STATUS_FILL[NodeStatus.DONE], _STATUS_STROKE[NodeStatus.DONE]),
        ("superseded 已作废", _STATUS_FILL[NodeStatus.SUPERSEDED], _STATUS_STROKE[NodeStatus.SUPERSEDED]),
        ("expired 已过期", _STATUS_FILL[NodeStatus.EXPIRED], _STATUS_STROKE[NodeStatus.EXPIRED]),
    ]
    spans = "".join(
        f'<span class="chip" style="background:{fill};border-color:{stroke}">{escape(label)}</span>'
        for label, fill, stroke in items
    )
    edges = "".join(
        f'<span class="chip"><i style="border-color:{color};border-style:{"dashed" if dash else "solid"}"></i>'
        f"{escape(edge_type.value)}</span>"
        for edge_type, (color, dash) in _EDGE_STYLE.items()
    )
    return f'<div class="legend">{spans}</div><div class="legend">{edges}</div>'


def graph_to_html(engine: GraphMemoryEngine, title: str = "Atlas 图状态快照") -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(node.id)}</td><td>{escape(node.type.value)}</td>"
        f"<td>{escape(node.status.value)}</td><td>{escape(node.actor)}</td>"
        f"<td>{escape(node.content)}</td><td>{escape(node.note)}</td>"
        "</tr>"
        for node in engine.nodes.values()
    )
    edges_rows = "".join(
        f"<tr><td>{escape(e.src)}</td><td>{escape(e.type.value)}</td><td>{escape(e.dst)}</td></tr>"
        for e in engine.edges
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", Helvetica, Arial, sans-serif; margin: 24px; color: #263238; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; margin-top: 28px; }}
.legend {{ margin: 6px 0; }}
.chip {{ display: inline-block; padding: 2px 8px; margin-right: 6px; border: 1.4px solid #90a4ae;
         border-radius: 10px; font-size: 12px; background: #fff; }}
.chip i {{ display: inline-block; width: 16px; height: 0; border-width: 2px; margin-right: 6px;
           vertical-align: middle; }}
table {{ border-collapse: collapse; font-size: 12.5px; margin-top: 6px; }}
td, th {{ border: 1px solid #cfd8dc; padding: 4px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f5f7f8; }}
</style></head><body>
<h1>{escape(title)}</h1>
{_legend_html()}
{graph_to_svg(engine)}
<h2>节点（{len(engine.nodes)}）</h2>
<table><tr><th>ID</th><th>类型</th><th>状态</th><th>写入者</th><th>内容</th><th>备注/原因</th></tr>{rows}</table>
<h2>边（{len(engine.edges)}）</h2>
<table><tr><th>源</th><th>类型</th><th>目标</th></tr>{edges_rows}</table>
</body></html>
"""


def export_graph(graph_path: str, out_path: str) -> str:
    """把引擎 JSONL 导出为 .svg 或 .html；返回输出路径。"""
    engine = GraphMemoryEngine.load_jsonl(graph_path)
    lowered = out_path.lower()
    if lowered.endswith(".svg"):
        content = graph_to_svg(engine)
    elif lowered.endswith(".html"):
        content = graph_to_html(engine)
    else:
        raise ValueError(f"不支持的输出格式（用 .svg 或 .html）：{out_path}")
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return out_path
