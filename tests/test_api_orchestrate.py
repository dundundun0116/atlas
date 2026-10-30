"""任务 API 的完整编排端点：Planner → Research → Worker → Auditor → 状态同步。

模型与检索工具都用替身：验证的是 Atlas 的编排接线、写图、状态生命周期与失败语义。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult

PLAN = json.dumps({"route": "先检索再作答", "risks": "证据不足", "reason": "事实型问题"})
AUDIT = json.dumps({"verdict": "ok", "issues": ""})


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        return self.replies.pop(0)


class FakeTool:
    def research(self, request):
        return ResearchResult(
            answer="1879 年",
            evidence=[EvidenceItem(source="Albert Einstein", chunk_id="c1",
                                   quote="1879 年出生于乌尔姆。", score=0.9,
                                   retriever="bm25", rank=1)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id="compass-trace-1",
        )


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "orch.db")
    yield database
    database.close()


def _client(db, *, llm="default", tool="default"):
    if llm == "default":
        llm = ScriptedLLM([PLAN, "编排草稿答案", AUDIT])
    if tool == "default":
        tool = FakeTool()
    return TestClient(create_app(GraphRepository(db), research_tool=tool,
                                 orchestrate_llm=llm))


def _create(client, session="s1", goal="回答出生年份"):
    assert client.post("/tasks", json={"session_id": session, "goal": goal}).status_code == 200


class TestOrchestrateEndpoint:
    def test_full_flow_writes_nodes_and_completes(self, db):
        client = _client(db)
        _create(client)
        body = client.post("/tasks/s1/orchestrate", json={"problem": "爱因斯坦出生于哪一年？"}).json()

        assert body["answer"] == "编排草稿答案"
        assert body["state"] == "completed"
        assert body["route"] == "先检索再作答"
        assert "planner" in body["calls"] and "auditor" in body["calls"]
        assert body["fact_ids"]
        assert body["research_trace_id"] == "compass-trace-1"

        nodes = client.get("/tasks/s1").json()["nodes"]
        types = {n["type"] for n in nodes}
        assert {"decision", "fact", "artifact"} <= types
        research = [n for n in nodes if n["type"] == "fact" and n["actor"] == "research"]
        assert research and json.loads(research[0]["evidence"])[0]["chunk_id"] == "c1"

    def test_second_call_on_terminal_task_is_rejected(self, db):
        client = _client(db)
        _create(client)
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 200
        response = client.post("/tasks/s1/orchestrate", json={})
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "state_conflict"

    def test_requires_planning_state(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/advance", json={"trigger": "plan_ready", "reason": "手动推进"})
        response = client.post("/tasks/s1/orchestrate", json={})
        assert response.status_code == 409

    def test_without_llm_returns_unrecoverable(self, db):
        client = TestClient(create_app(GraphRepository(db), orchestrate_llm=None))
        _create(client)
        response = client.post("/tasks/s1/orchestrate", json={})
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "unrecoverable"

    def test_model_failure_marks_task_failed(self, db):
        def boom(_prompt):
            raise RuntimeError("endpoint down")

        client = _client(db, llm=boom)
        _create(client)
        response = client.post("/tasks/s1/orchestrate", json={})
        assert response.status_code == 500
        assert client.get("/tasks/s1").json()["state"] == "failed"
