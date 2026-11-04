"""恢复快照与报告（设计文档 §4.13）。

两件事必须能回答：
1. **崩溃后靠什么恢复**——checkpoint 里要有"还没收敛的 Step、在途工具调用、预算"；
2. **人怎么看出发生了什么**——报告与 CLI 要能看到计划、每步状态、工具调用与对账痕迹。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.runtime import AgentRuntime
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult
from atlas.tools.protocol import ResearchToolError

AUDIT = json.dumps({"verdict": "ok", "issues": "", "refs": ["c1"]})


class ScriptedLLM:
    def __call__(self, prompt):
        if "revising your draft" in prompt:
            return "修订稿"
        return AUDIT if "AUDITOR" in prompt else "爱因斯坦出生于 1879 年。"


class FakeTool:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def research(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ResearchResult(
            answer="1879 年",
            evidence=[EvidenceItem(source="Einstein", chunk_id="c1",
                                   quote="1879 年出生于乌尔姆。", score=0.9,
                                   retriever="bm25", rank=1)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id="compass-trace-1",
        )


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "checkpoint.db")
    yield database
    database.close()


def _client(db, tool):
    return TestClient(create_app(
        GraphRepository(db), research_tool=tool, orchestrate_llm=ScriptedLLM(),
        context_token_counter=lambda text: (len(text) + 3) // 4,
    ))


def _run_dag(client, session="s1"):
    client.post("/tasks", json={"session_id": session, "goal": "q"})
    client.post(f"/tasks/{session}/plans",
                json={"problem": "爱因斯坦出生于哪一年？", "activate": True})
    return client.post(f"/tasks/{session}/plans/P1/advance", json={}).json()


class TestCheckpointSnapshots:
    def test_checkpoint_records_pending_steps_and_budget(self, db):
        client = _client(db, FakeTool())
        _run_dag(client)
        checkpoint = client.get("/tasks/s1").json()["checkpoint"]
        assert checkpoint["task_state"] == "completed"
        # 全部 done ⇒ 没有未收敛的 Step
        assert checkpoint["pending_step_ids"] == []
        assert checkpoint["in_flight_tool_call_ids"] == []

    def test_in_flight_tool_call_is_recorded_for_recovery(self, db):
        client = _client(db, FakeTool(error=ResearchToolError("timeout", "超时")))
        body = _run_dag(client)
        assert body["state"] == "paused"

        checkpoint = client.get("/tasks/s1").json()["checkpoint"]
        assert "retrieve" in checkpoint["pending_step_ids"]
        assert len(checkpoint["in_flight_tool_call_ids"]) == 1
        # 在途调用 id 必须真的是那条 indeterminate 的记录
        call_id = checkpoint["in_flight_tool_call_ids"][0]
        rows = GraphRepository(db).tool_calls("s1")
        assert [row["tool_call_id"] for row in rows] == [call_id]
        assert rows[0]["status"] == "indeterminate"

    def test_service_checkpoint_is_filled_automatically(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        service = ServiceRuntime(repo, "s1")
        step = repo.save_plan
        from atlas.planning.research_plan import build_research_plan
        spec = build_research_plan("问题")
        repo.save_plan("s1", spec)
        repo.activate_plan("s1", "P1")
        repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1})
        row = service.checkpoint(pending_role="worker")
        assert sorted(row.pending_step_ids) == ["draft", "final_verify", "repair",
                                                "retrieve", "verify"]
        assert row.in_flight_tool_call_ids == []
        assert row.budget_snapshot == {}


class TestReport:
    def test_html_report_shows_plan_and_tool_calls(self, db):
        client = _client(db, FakeTool())
        _run_dag(client)
        html = (
            __import__("atlas.report", fromlist=["render_task_report"])
            .render_task_report(
                state=client.get("/tasks/s1").json(),
                trace=client.get("/tasks/s1/trace").json(),
                replay=client.get("/tasks/s1/replay").json(),
                nodes=GraphRepository(db).nodes("s1"),
                plan=GraphRepository(db).plan_detail("s1", "P1"),
                tool_calls=GraphRepository(db).tool_calls("s1"),
            )
        )
        assert "计划与步骤" in html
        assert "retrieve" in html and "final_verify" in html
        assert "工具调用与对账" in html
        assert "compassrag" in html
        assert "settled" in html

    def test_report_without_plan_still_renders(self, db):
        client = _client(db, FakeTool())
        client.post("/tasks", json={"session_id": "s1", "goal": "q"})
        from atlas.report import render_task_report

        html = render_task_report(
            state=client.get("/tasks/s1").json(),
            trace=client.get("/tasks/s1/trace").json(),
            replay=client.get("/tasks/s1/replay").json(),
            nodes=GraphRepository(db).nodes("s1"),
        )
        assert "没有激活的计划" in html
        assert "没有外部工具调用记录" in html

    def test_html_escapes_tool_names(self, db):
        from atlas.report import render_task_report

        html = render_task_report(
            state={"session_id": "s1", "goal": "g", "state": "running"},
            trace={}, replay={}, nodes=[],
            tool_calls=[{"tool_name": "<img src=x onerror=alert(1)>",
                         "step_id": "s", "status": "settled",
                         "outcome": "applied", "retry_count": 0,
                         "reconcile_attempts": 0, "idempotency_key": "k"}],
        )
        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_text_report_lists_plan_and_calls(self, db):
        client = _client(db, FakeTool())
        _run_dag(client)
        runtime = AgentRuntime(GraphRepository(db))
        report = runtime.get_report("s1")["report"]
        assert "计划: P1" in report
        assert "retrieve" in report and "[done]" in report
        assert "工具调用:" in report
        assert "compassrag" in report
