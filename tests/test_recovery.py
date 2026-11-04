"""按持久化状态恢复（设计文档 §4.13 / §5.5）。

恢复是最容易造成重复副作用的动作，所以它的第一职责不是"接着跑"，而是
**先判断能不能跑**：有在途未知调用就停下，有人工闸门就停下，有 stale 就停下。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.planning.recovery import assess_recovery
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult

AUDIT = json.dumps({"verdict": "ok", "issues": "", "refs": ["c1"]})
PROBLEM = "爱因斯坦出生于哪一年？"


class ScriptedLLM:
    def __call__(self, prompt):
        if "revising your draft" in prompt:
            return "修订稿"
        return AUDIT if "AUDITOR" in prompt else "爱因斯坦出生于 1879 年。"


class FakeTool:
    def __init__(self):
        self.calls = 0

    def research(self, request):
        self.calls += 1
        return ResearchResult(
            answer="1879 年",
            evidence=[EvidenceItem(source="Einstein", chunk_id="c1",
                                   quote="1879 年出生于乌尔姆。", score=0.9,
                                   retriever="bm25", rank=1)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id="ct-1",
        )


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "recovery.db")
    yield database
    database.close()


def _client(db, tool=None):
    return TestClient(create_app(
        GraphRepository(db), research_tool=tool or FakeTool(),
        orchestrate_llm=ScriptedLLM(),
        context_token_counter=lambda text: (len(text) + 3) // 4,
    ))


def _start(client, session="s1"):
    client.post("/tasks", json={"session_id": session, "goal": PROBM if False else PROBLEM})
    client.post(f"/tasks/{session}/plans", json={"problem": PROBLEM, "activate": True})


PROBM = PROBLEM


class TestAssessment:
    def test_no_active_plan_is_nothing_to_do(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        status = assess_recovery(repo, "s1")
        assert status.action == "nothing_to_do"
        assert status.plan_id == ""
        assert "没有激活的计划" in status.reason

    def test_fresh_active_plan_is_resumable(self, db):
        client = _client(db)
        _start(client)
        status = assess_recovery(GraphRepository(db), "s1")
        assert status.action == "resume"
        assert status.blocked is False
        assert set(status.pending_steps) == {"retrieve", "draft", "verify",
                                             "repair", "final_verify"}

    def test_dispatched_tool_call_blocks_recovery(self, db):
        client = _client(db)
        _start(client)
        repo = GraphRepository(db)
        repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1},
                               step_id="retrieve", plan_id="P1")
        call = repo.tool_calls("s1")[0]
        repo.dispatch_tool_call("s1", call["tool_call_id"])

        status = assess_recovery(repo, "s1")
        assert status.blocked is True
        assert status.action == "blocked_on_reconcile"
        assert status.in_flight_tool_calls[0]["tool_call_id"] == call["tool_call_id"]


class TestRecoverEndpoint:
    def test_dry_run_does_not_execute_anything(self, db):
        tool = FakeTool()
        client = _client(db, tool)
        _start(client)
        body = client.post("/tasks/s1/plans/P1/recover", json={"dry_run": True}).json()
        assert body["action"] == "resume"
        assert body["executed"] == []
        assert tool.calls == 0

    def test_blocked_recovery_does_not_touch_the_tool(self, db):
        tool = FakeTool()
        client = _client(db, tool)
        _start(client)
        repo = GraphRepository(db)
        repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1},
                               step_id="retrieve", plan_id="P1")
        call = repo.tool_calls("s1")[0]
        repo.dispatch_tool_call("s1", call["tool_call_id"])

        body = client.post("/tasks/s1/plans/P1/recover", json={}).json()
        assert body["action"] == "blocked_on_reconcile"
        assert body["executed"] == []
        assert body["blocked"] is True
        assert tool.calls == 0, "有在途未知调用时绝不能开跑"

    def test_reconcile_then_recover_completes(self, db):
        tool = FakeTool()
        client = _client(db, tool)
        _start(client)
        repo = GraphRepository(db)
        repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1},
                               step_id="retrieve", plan_id="P1")
        call = repo.tool_calls("s1")[0]
        repo.dispatch_tool_call("s1", call["tool_call_id"])

        # 派发后崩溃的调用也必须能对账（先落成 unknown_commit 再定论）
        reconciled = client.post(
            f"/tasks/s1/tool-calls/{call['tool_call_id']}/reconcile",
            json={"outcome": "applied", "detail": "人工确认检索已返回"},
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "reconciled"

        body = client.post("/tasks/s1/plans/P1/recover", json={}).json()
        assert body["action"] == "resume"
        assert body["complete"] is True
        assert body["state"] == "completed"
        assert set(body["executed"]) == {"retrieve", "draft", "verify", "repair", "final_verify"}

    def test_partial_progress_only_reruns_unfinished(self, db):
        """跑到一半崩掉：已完成的 Step 不重跑，只补剩下的。"""
        client = _client(db)
        _start(client)
        repo = GraphRepository(db)
        for step_id in ("retrieve",):
            repo.claim_step("s1", "P1", step_id)
            repo.set_step_status("s1", "P1", step_id, "running")
            repo.set_step_status("s1", "P1", step_id, "verifying")
            repo.set_step_status("s1", "P1", step_id, "done")
        repo.set_step_status("s1", "P1", "draft", "pending", reason="依赖已满足")

        body = client.post("/tasks/s1/plans/P1/recover", json={}).json()
        assert "retrieve" not in body["executed"]
        assert body["executed"][0] == "draft"
        assert body["complete"] is True

    def test_needs_human_blocks_recovery(self, db):
        client = _client(db)
        _start(client)
        repo = GraphRepository(db)
        # retrieve 没有依赖，初始就是 pending；直接转人工闸门
        repo.set_step_status("s1", "P1", "retrieve", "needs_human", reason="判不了")

        body = client.post("/tasks/s1/plans/P1/recover", json={}).json()
        assert body["action"] == "blocked_on_human"
        assert body["needs_human_steps"] == ["retrieve"]
        assert body["executed"] == []

    def test_stale_step_asks_for_rebinding(self, db):
        client = _client(db)
        _start(client)
        repo = GraphRepository(db)
        Plan = client.post("/tasks/s1/plans/P1/advance", json={})
        assert Plan.status_code == 200
        repo.mark_downstream_stale("s1", "P1", ["draft"], reason="上游换了")

        body = client.post("/tasks/s1/plans/P1/recover", json={"dry_run": True}).json()
        assert "verify" in body["stale_steps"]
        assert body["action"] == "blocked_on_human"
        assert "重新绑定" in body["reason"]

    def test_only_active_plan_can_be_recovered(self, db):
        client = _client(db)
        client.post("/tasks", json={"session_id": "s1", "goal": "q"})
        client.post("/tasks/s1/plans", json={"problem": PROBLEM})  # 不激活
        response = client.post("/tasks/s1/plans/P1/recover", json={})
        assert response.status_code == 409
        assert "只有 active" in response.json()["error"]["message"]

    def test_unknown_plan_is_404(self, db):
        client = _client(db)
        _start(client)
        assert client.post("/tasks/s1/plans/P9/recover", json={}).status_code == 404

    def test_completed_plan_has_nothing_to_do(self, db):
        client = _client(db)
        _start(client)
        client.post("/tasks/s1/plans/P1/advance", json={})
        body = client.post("/tasks/s1/plans/P1/recover", json={}).json()
        assert body["action"] == "nothing_to_do"
        assert body["executed"] == []
        assert body["state"] == "completed"
