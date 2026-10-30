"""统一任务报告：把状态、角色步骤、证据与 CompassRAG trace 收在一页（静态 HTML）。"""

import json
from pathlib import Path

from atlas.cli import main as cli_main
from atlas.report import render_task_report
from atlas.runtime import AgentRuntime
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database

EVIDENCE = [
    {"source": "Albert Einstein", "chunk_id": "c1", "quote": "1879 年出生于乌尔姆。",
     "score": 0.9, "retriever": "bm25", "rank": 1},
    {"source": "狭义相对论", "chunk_id": "c2", "quote": "1905 年提出。",
     "score": 0.7, "retriever": "kg", "rank": 2},
]


def _build_db(tmp_path) -> str:
    db_path = str(tmp_path / "task.db")
    database = open_database(db_path)
    repository = GraphRepository(database)
    runtime = AgentRuntime(repository)
    runtime.create_task("s1", "回答 <script>alert(1)</script> 之外的问题", idempotency_key="c1")
    runtime.advance_task("s1", "plan_ready", reason="先检索再作答", idempotency_key="p1")

    service = ServiceRuntime(repository, "s1")
    service.note("planner", "先查出生年，再查相对论", kind="decision", reason="事实型问题")
    service.note("research", "1879 年出生；1905 年提出狭义相对论", kind="fact",
                 evidence=json.dumps(EVIDENCE, ensure_ascii=False),
                 source="Albert Einstein,狭义相对论", confidence=0.8,
                 meta={"research_trace_id": "abcdef123456", "research_stop_reason": "answered",
                       "research_rounds": 1, "research_confidence": 0.8})
    service.note("worker", "草稿：1879 / 1905", kind="artifact")
    repository.record_trace("s1", {"trace_id": "t1", "agent_role": "research",
                                   "tool_name": "compassrag", "outcome": "ok",
                                   "latency_ms": 12.5, "token_usage": 30})
    database.close()
    return db_path


def _load(db_path: str):
    database = open_database(db_path)
    try:
        repository = GraphRepository(database)
        runtime = AgentRuntime(repository)
        return (
            runtime.get_state("s1"),
            runtime.get_trace("s1"),
            runtime.replay("s1"),
            repository.nodes("s1"),
        )
    finally:
        database.close()


def test_report_contains_evidence_and_research_trace(tmp_path):
    db_path = _build_db(tmp_path)
    state, trace, replay, nodes = _load(db_path)
    html = render_task_report(
        state=state, trace=trace, replay=replay, nodes=nodes,
        research_base_url="http://127.0.0.1:8000",
    )

    assert "Atlas 任务报告" in html
    assert "advance_task" in html and "add_node" in html   # 任务时间线
    assert "Albert Einstein" in html          # 证据来源
    assert "bm25" in html and "#1" in html    # 检索器与排名
    assert "abcdef123456" in html             # CompassRAG trace id
    assert "http://127.0.0.1:8000/runs/abcdef123456" in html  # 可下钻链接
    assert "research" in html                 # 角色步骤
    # 转义：目标里的 <script> 不能变成真标签
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_cli_writes_report_file(tmp_path):
    db_path = _build_db(tmp_path)
    out = tmp_path / "reports" / "s1.html"
    code = cli_main(["report", "s1", "--db", db_path, "--out", str(out),
                     "--research-base-url", "http://localhost:9000"])
    assert code == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "CompassRAG" in text and "abcdef123456" in text
