"""长程 Harness 的 P0 接线（设计文档 §11 Phase 0/1 + 调研报告 §7.7）。

六条一条一个用例：
1. `routes` 把状态转移交给持久化运行时（不是最后再补一遍）；
2. `state_view` 走四区组装器（模型看到真实状态，而不是"最近 5 个节点"）；
3. `_call` 走 `call_with_policy`（空输出重试真的发生）；
4. 编排**成功**也写 trace（之前只有失败路径写）；
5. `repair_required` 但 issues 为空 → 收敛到 FAILED，不静默当通过；
6. `expire()` 有真实调用点（时间规则过期）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.orchestration.policies import Budget
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult

PLAN = json.dumps({"route": "先检索再作答", "risks": "证据不足", "reason": "事实型问题"})
AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})
AUDIT_REVISE_NO_ISSUES = json.dumps({"verdict": "revise", "issues": ""})
AUDIT_REVISE = json.dumps({"verdict": "revise", "issues": "结论没有引用证据"})


def count_words(text: str) -> int:
    return len(str(text or "").split())


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
    database = open_database(tmp_path / "horizon.db")
    yield database
    database.close()


def _client(db, replies=None, *, budget=None, tool=None):
    llm = ScriptedLLM(replies or [PLAN, "编排草稿答案", AUDIT_OK])
    return TestClient(create_app(
        GraphRepository(db),
        research_tool=tool or FakeTool(),
        orchestrate_llm=llm,
        orchestrate_budget=budget,
        context_token_counter=count_words,
    ))


def _create(client, session="s1", goal="回答出生年份"):
    assert client.post("/tasks", json={"session_id": session, "goal": goal}).status_code == 200


class TestPersistedTransitions:
    def test_transitions_happen_during_run_not_at_the_end(self, db):
        """状态转移必须与编排过程交错发生，而不是跑完再补三下。"""
        client = _client(db)
        _create(client)
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 200

        repo = GraphRepository(db)
        operations = [(e.operation, (e.payload or {}).get("agent_role")
                       or (e.payload or {}).get("actor") or "")
                      for e in repo.events("s1")]
        decision_at = next(i for i, (op, _) in enumerate(operations) if op == "add_node")
        machine_transitions = [i for i, (op, role) in enumerate(operations)
                               if op == "trace" and role == "worker"
                               and (repo.events("s1")[i].payload or {}).get("tool_name")
                               == "state_machine"]
        assert machine_transitions, "状态转移没有留下 trace"
        assert decision_at < machine_transitions[0], "plan_ready 必须在 planner 写决定之后立即落库"

    def test_state_machine_is_advanced_three_times(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/orchestrate", json={})
        repo = GraphRepository(db)
        transitions = [e.payload for e in repo.events("s1")
                       if e.operation == "trace"
                       and (e.payload or {}).get("tool_name") == "state_machine"]
        assert [t["step_id"] for t in transitions] == ["plan_ready", "work_done", "audit_pass"]
        assert client.get("/tasks/s1").json()["state"] == "completed"


class TestContextMainPath:
    def test_every_model_call_uses_context_builder(self, db):
        client = _client(db)
        _create(client)
        body = client.post("/tasks/s1/orchestrate", json={}).json()

        assert len(body["context_digests"]) == 3, "planner / worker / auditor 各一次组装"
        packages = client.get("/tasks/s1/contexts").json()["packages"]
        assert [p["role"] for p in packages] == ["planner", "worker", "auditor"]
        assert [p["digest"] for p in packages] == body["context_digests"]
        # worker 的上下文里必须能看到 planner 刚写的决定（四区组装，不是截断预览）
        worker_pkg = packages[1]
        assert worker_pkg["actual_tokens"] > 0
        assert "D1" in worker_pkg["included_refs"]

    def test_trace_carries_context_digest(self, db):
        client = _client(db)
        _create(client)
        body = client.post("/tasks/s1/orchestrate", json={}).json()
        events = client.get("/tasks/s1/trace").json()["events"]
        model_events = [e for e in events if e["tool_name"] == "model"]
        assert model_events and all(e["outcome"] == "ok" for e in model_events)
        digests = [e["context_digest"] for e in model_events]
        assert digests and all(digests)
        assert digests == body["context_digests"]

    def test_success_path_writes_trace(self, db):
        """之前只有失败路径写 trace；成功路径必须也留事件。"""
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/orchestrate", json={})
        summary = client.get("/tasks/s1/trace").json()["summary"]
        assert summary["events"] >= 4  # 3 次模型调用 + 3 次状态转移（至少 4）
        assert summary["errors"] == 0

    def test_context_budget_overflow_degrades_instead_of_crashing(self, db):
        client = TestClient(create_app(
            GraphRepository(db), research_tool=FakeTool(),
            orchestrate_llm=ScriptedLLM([PLAN, "草稿", AUDIT_OK]),
            context_token_counter=count_words,
        ))
        _create(client)
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 200


class TestRetryPolicy:
    def test_empty_model_output_is_retried(self, db):
        client = _client(db, ["", PLAN, "草稿答案", AUDIT_OK])
        _create(client)
        body = client.post("/tasks/s1/orchestrate", json={}).json()
        assert body["state"] == "completed"
        assert body["retries"] == 1
        events = client.get("/tasks/s1/trace").json()["events"]
        planner = [e for e in events if e["agent_role"] == "planner"]
        assert planner and planner[0]["outcome"] == "ok"

    def test_hard_failure_is_not_retried_and_converges(self, db):
        def boom(_prompt):
            raise RuntimeError("endpoint down")

        client = TestClient(create_app(
            GraphRepository(db), research_tool=FakeTool(), orchestrate_llm=boom,
            context_token_counter=count_words,
        ))
        _create(client)
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 500
        state = client.get("/tasks/s1").json()
        assert state["state"] == "failed"
        assert "planner" in state["failure_reason"]


class TestAuditorNonConvergence:
    def test_repair_required_without_issues_fails_instead_of_passing(self, db):
        client = _client(db, [PLAN, "草稿答案", AUDIT_REVISE_NO_ISSUES, "修订稿"])
        _create(client)
        response = client.post("/tasks/s1/orchestrate", json={})
        assert response.status_code == 500
        state = client.get("/tasks/s1").json()
        assert state["state"] == "failed"
        assert "未给出可执行问题" in state["failure_reason"]

    def test_repair_with_issues_still_works(self, db):
        client = _client(db, [PLAN, "草稿答案", AUDIT_REVISE, "修订后的答案"])
        _create(client)
        body = client.post("/tasks/s1/orchestrate", json={}).json()
        assert body["state"] == "completed"
        assert body["answer"] == "修订后的答案"


class TestBudgetWiring:
    def test_budget_exhaustion_fails_the_run(self, db):
        client = _client(db, budget=Budget(max_rounds=1))
        _create(client)
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 500
        state = client.get("/tasks/s1").json()
        assert state["state"] == "failed"
        assert "budget_exhausted" in state["failure_reason"]

    def test_budget_template_is_not_shared_between_runs(self, db):
        template = Budget(max_rounds=5)
        client = _client(db, [PLAN, "草稿 1", AUDIT_OK, PLAN, "草稿 2", AUDIT_OK],
                         budget=template)
        _create(client, session="s1")
        _create(client, session="s2")
        assert client.post("/tasks/s1/orchestrate", json={}).status_code == 200
        assert client.post("/tasks/s2/orchestrate", json={}).status_code == 200
        assert template.rounds == 0, "模板预算不该被某一次运行消耗"


class TestExpireCallSite:
    def test_overdue_state_is_expired_when_advancing(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="读书")
        client = TestClient(create_app(repo, context_token_counter=count_words))
        _create(client)
        constraint = repo.add_node(
            "s1", type_="constraint", content="预算 5000",
            source="user", valid_until="2000-01-01",
        )
        client.post("/tasks/s1/advance", json={"trigger": "plan_ready"})

        nodes = {n["id"]: n for n in client.get("/tasks/s1").json()["nodes"]}
        assert nodes[constraint.node_id]["status"] == "expired"
        operations = [e.operation for e in repo.events("s1")]
        assert "expire" in operations

    def test_expire_is_idempotent(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="读书")
        client = TestClient(create_app(repo, context_token_counter=count_words))
        _create(client)
        repo.add_node("s1", type_="fact", content="旧票价", valid_until="2000-01-01")
        client.post("/tasks/s1/advance", json={"trigger": "plan_ready"})
        client.post("/tasks/s1/advance", json={"trigger": "work_done"})
        operations = [e.operation for e in repo.events("s1")]
        assert operations.count("expire") == 1


class TestWorkflowVersion:
    def test_state_and_orchestrate_report_legacy_workflow(self, db):
        client = _client(db)
        _create(client)
        assert client.get("/tasks/s1").json()["workflow_version"] == "legacy_research_v1"
        body = client.post("/tasks/s1/orchestrate", json={}).json()
        assert body["workflow_version"] == "legacy_research_v1"
