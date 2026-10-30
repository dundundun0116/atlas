"""任务 API 的研究端点：Atlas 调用 ResearchTool → 写事实（证据/trace 可下钻）。

工具本身用替身：这里验证的是**Atlas 侧写图与失败语义**，不是检索质量。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult, ResearchToolError


class FakeTool:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def research(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result or ResearchResult(
            answer="1879 年",
            evidence=[EvidenceItem(source="Albert Einstein", chunk_id="c1",
                                   quote="1879 年出生于乌尔姆。", score=0.9,
                                   retriever="bm25", rank=1)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id="compass-trace-1",
        )


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "research.db")
    yield database
    database.close()


def _client(db, tool="default"):
    repository = GraphRepository(db)
    if tool == "default":
        tool = FakeTool()
    return TestClient(create_app(repository, research_tool=tool)), repository


def _create_task(client, session="s1"):
    response = client.post("/tasks", json={"session_id": session, "goal": "回答出生年份"})
    assert response.status_code == 200


class TestResearchEndpoint:
    def test_success_writes_fact_with_evidence_and_trace(self, db):
        client, repository = _client(db)
        _create_task(client)
        body = client.post("/tasks/s1/research", json={"query": "爱因斯坦出生于哪一年？"}).json()
        assert body["fact_id"]
        assert body["answer"] == "1879 年"
        assert body["evidence"][0]["retriever"] == "bm25" and body["evidence"][0]["rank"] == 1
        assert body["trace_id"] == "compass-trace-1"

        nodes = client.get("/tasks/s1").json()["nodes"]
        fact = next(n for n in nodes if n["id"] == body["fact_id"])
        assert fact["type"] == "fact" and fact["actor"] == "research"
        assert json.loads(fact["evidence"])[0]["chunk_id"] == "c1"
        assert fact["meta"]["research_trace_id"] == "compass-trace-1"

    def test_idempotent_retry_does_not_duplicate_fact(self, db):
        client, _ = _client(db)
        _create_task(client)
        first = client.post("/tasks/s1/research", json={"query": "q"}).json()
        second = client.post("/tasks/s1/research", json={"query": "q"}).json()
        assert first["fact_id"] == second["fact_id"]
        facts = [n for n in client.get("/tasks/s1").json()["nodes"] if n["type"] == "fact"]
        assert len(facts) == 1

    def test_tool_failure_returns_structured_kind_and_no_fact(self, db):
        tool = FakeTool(error=ResearchToolError("no_evidence", "库里没有相关文档", trace_id="t-9"))
        client, _ = _client(db, tool)
        _create_task(client)
        body = client.post("/tasks/s1/research", json={"query": "不相关的问题"}).json()
        assert body["failure_kind"] == "no_evidence"
        assert body["fact_id"] == ""
        assert body["trace_id"] == "t-9"
        assert not [n for n in client.get("/tasks/s1").json()["nodes"] if n["type"] == "fact"]

    def test_tool_timeout_returns_504_model_timeout(self, db):
        tool = FakeTool(error=ResearchToolError("timeout", "upstream slow", trace_id="t-slow"))
        client, _ = _client(db, tool)
        _create_task(client)
        response = client.post("/tasks/s1/research", json={"query": "q"})
        assert response.status_code == 504
        assert response.json()["error"]["type"] == "model_timeout"
        traces = client.get("/tasks/s1/trace").json()["events"]
        assert any(event.get("outcome") == "timeout" for event in traces)

    def test_without_tool_returns_unrecoverable(self, db):
        client, _ = _client(db, tool=None)
        _create_task(client)
        response = client.post("/tasks/s1/research", json={"query": "q"})
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "unrecoverable"
