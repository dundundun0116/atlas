"""统一任务报告：把 Atlas 的任务状态、角色步骤、证据链与 CompassRAG trace 收在一页。

`AgentRuntime.get_report()` 给的是纯文本摘要；这里补一个**静态 HTML 入口**，
面向「演示 / 复盘 / 面试现场」：一张页面回答任务做到哪、每一步是谁做的、
关键结论引用了什么材料、以及每次研究调用对应的 CompassRAG `trace_id`。

设计约束：
- 纯静态、零运行时依赖（不引模板引擎），离线可渲染；
- CompassRAG trace 只给 `trace_id` 与可选链接，不复制其内部轨迹（边界不变）；
- 转义所有渲染内容，避免把模型输出当 HTML。
"""

from __future__ import annotations

import html
import json
import os
from typing import Any, Dict, Iterable, List

_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", "PingFang SC", sans-serif;
       margin: 0 auto; max-width: 1080px; padding: 32px 24px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 10px; border-bottom: 1px solid #8884; padding-bottom: 6px; }
.sub { opacity: .7; font-size: 13px; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
.card { border: 1px solid #8883; border-radius: 10px; padding: 10px 14px; min-width: 120px; }
.card b { display: block; font-size: 20px; }
.card span { font-size: 12px; opacity: .7; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #8882; vertical-align: top; }
code { font-size: 12.5px; background: #8881; padding: 1px 4px; border-radius: 4px; }
.evidence { margin: 8px 0 0 0; padding-left: 18px; }
.evidence li { margin-bottom: 4px; }
.muted { opacity: .65; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _evidence_items(raw: str) -> List[Dict[str, Any]]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _collect_research_traces(nodes: Iterable[Any]) -> List[Dict[str, Any]]:
    """从研究 Fact 的 meta 里收集 CompassRAG trace（Atlas 只存 id 与摘要，不复制轨迹）。"""
    found: List[Dict[str, Any]] = []
    for node in nodes:
        meta = getattr(node, "meta", None) or {}
        trace_id = str(meta.get("research_trace_id") or "").strip()
        if trace_id:
            found.append({
                "trace_id": trace_id,
                "fact_id": getattr(node, "node_id", ""),
                "stop_reason": str(meta.get("research_stop_reason") or ""),
                "rounds": meta.get("research_rounds", 0),
                "confidence": meta.get("research_confidence", 0.0),
            })
    return found


def _render_roles(nodes: List[Any]) -> str:
    roles = {}
    for node in nodes:
        roles.setdefault(getattr(node, "actor", "main"), []).append(node)
    rows = []
    for role in sorted(roles):
        items = roles[role]
        active = sum(1 for n in items if n.status == "active")
        rows.append(
            f"<tr><td><b>{_e(role)}</b></td><td>{len(items)}</td>"
            f"<td>{active}</td><td>{_e(', '.join(sorted({n.type for n in items})))}</td></tr>"
        )
    return (
        "<table><tr><th>角色</th><th>节点数</th><th>活跃</th><th>节点类型</th></tr>"
        + "".join(rows) + "</table>"
    )


def _render_facts(nodes: List[Any]) -> str:
    facts = [n for n in nodes if n.type in ("fact", "artifact", "decision")]
    rows = []
    for node in facts:
        status = f"{node.status}"
        evidence = _evidence_items(node.evidence)
        detail = ""
        if evidence:
            items = "".join(
                f"<li><b>{_e(item.get('source') or item.get('chunk_id'))}</b>"
                f" <span class='muted'>[{_e(item.get('retriever') or 'retriever?')}"
                f" #{_e(item.get('rank') or 0)}]</span> — {_e(item.get('quote'))}</li>"
                for item in evidence
            )
            detail = f"<ul class='evidence'>{items}</ul>"
        rows.append(
            f"<tr><td><code>{_e(node.node_id)}</code></td><td>{_e(node.type)}</td>"
            f"<td>{_e(node.actor)}</td><td>{_e(status)}</td>"
            f"<td>{_e(node.content)}{detail}</td></tr>"
        )
    if not rows:
        return "<p class='muted'>没有决策 / 事实 / 产出物。</p>"
    return (
        "<table><tr><th>id</th><th>类型</th><th>角色</th><th>状态</th><th>内容与依据</th></tr>"
        + "".join(rows) + "</table>"
    )


def _render_timeline(replay: Dict[str, Any], limit: int = 80) -> str:
    events = list(replay.get("timeline") or [])[-limit:]
    if not events:
        return "<p class='muted'>没有业务事件。</p>"
    rows = [
        f"<tr><td>{_e(e.get('seq', ''))}</td><td>{_e(e.get('operation', ''))}</td>"
        f"<td>{_e(e.get('actor', ''))}</td>"
        f"<td><code>{_e(json.dumps(e.get('payload') or {}, ensure_ascii=False)[:220])}</code></td></tr>"
        for e in events
    ]
    return (
        "<table><tr><th>seq</th><th>操作</th><th>角色</th><th>payload</th></tr>"
        + "".join(rows) + "</table>"
    )


def _render_research_traces(traces: List[Dict[str, Any]], base_url: str) -> str:
    if not traces:
        return "<p class='muted'>本任务没有 CompassRAG 研究轨迹（未启用 ResearchTool 或未成功）。</p>"
    rows = []
    for item in traces:
        link = ""
        if base_url:
            link = (f" <a href=\"{_e(base_url.rstrip('/'))}/runs/{_e(item['trace_id'])}\">"
                    f"查看检索轨迹</a>")
        rows.append(
            f"<tr><td><code>{_e(item['trace_id'])}</code>{link}</td>"
            f"<td>{_e(item['fact_id'])}</td><td>{_e(item['stop_reason'])}</td>"
            f"<td>{_e(item['rounds'])}</td><td>{float(item['confidence'] or 0):.2f}</td></tr>"
        )
    return (
        "<table><tr><th>CompassRAG trace</th><th>Atlas Fact</th><th>停止原因</th>"
        "<th>轮数</th><th>置信度</th></tr>" + "".join(rows) + "</table>"
    )


def render_task_report(
    *, state: Dict[str, Any], trace: Dict[str, Any], replay: Dict[str, Any],
    nodes: List[Any], research_base_url: str = "", title: str = "Atlas 任务报告",
) -> str:
    """渲染一页静态 HTML：状态 / 角色 / 证据 / CompassRAG trace 关联 / 时间线。"""
    summary = trace.get("summary") or {}
    traces = _collect_research_traces(nodes)
    active = sum(1 for n in nodes if getattr(n, "status", "") == "active")
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · {_e(state.get('session_id'))}</title>
<style>{_CSS}</style></head>
<body>
<h1>{_e(title)}</h1>
<div class="sub">会话 <code>{_e(state.get('session_id'))}</code>
 · 目标：{_e(state.get('goal'))}</div>

<div class="cards">
  <div class="card"><b>{_e(state.get('state'))}</b><span>任务状态</span></div>
  <div class="card"><b>{len(nodes)}</b><span>图节点（活跃 {active}）</span></div>
  <div class="card"><b>{_e(summary.get('events', 0))}</b><span>trace 事件</span></div>
  <div class="card"><b>{_e(summary.get('errors', 0))}</b><span>失败事件</span></div>
  <div class="card"><b>{len(traces)}</b><span>CompassRAG 研究轨迹</span></div>
  <div class="card"><b>{'OK' if replay.get('ok') else '异常'}</b><span>事件回放</span></div>
</div>

<h2>角色步骤</h2>
{_render_roles(nodes)}

<h2>决策 / 事实 / 产出物（含证据）</h2>
{_render_facts(nodes)}

<h2>研究轨迹（可下钻 CompassRAG）</h2>
{_render_research_traces(traces, research_base_url)}

<h2>任务时间线</h2>
{_render_timeline(replay)}
</body></html>"""
    return body


def write_task_report(path: str, **kwargs: Any) -> str:
    """渲染并落盘（自动建目录），返回写入路径。"""
    content = render_task_report(**kwargs)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path
