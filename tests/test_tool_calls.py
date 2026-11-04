"""Phase 3 验收：工具调用记录与 unknown 对账（设计文档 §4.8 / §11 Phase 3）。

五类必须覆盖的现场：
1. 发送**前**崩溃：只落了意图，重进可以安全派发；
2. 发送**后**响应丢失：可能已经生效，只能对账，**绝不重发**；
3. 重复响应：同一结论重复落库是幂等的；
4. 部分成功：`failed_partial` 只能走补偿；
5. 对账不可知：`unknowable` 保持在 indeterminate，升级给人。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools.calls import (
    EffectClass,
    IllegalToolCallTransition,
    Outcome,
    ToolCallStatus,
    ToolSpec,
    args_fingerprint,
    decide_from_observation,
    idempotency_key,
    transition,
)
from atlas.tools.dispatcher import ToolDispatcher
from atlas.tools.protocol import ResearchToolError


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "tool_calls_db.db")
    yield database
    database.close()


@pytest.fixture()
def repo(tmp_path):
    database = open_database(tmp_path / "tool_calls.db")
    repository = GraphRepository(database)
    repository.create_session("s1", goal="g")
    yield repository
    database.close()


class TestToolCallStateMachine:
    @pytest.mark.parametrize("source,target", [
        ("proposed", "dispatched"),
        ("dispatched", "settled"),
        ("dispatched", "failed_determinate"),
        ("dispatched", "failed_partial"),
        ("dispatched", "indeterminate"),
        ("indeterminate", "reconciling"),
        ("reconciling", "reconciled"),
        ("reconciling", "indeterminate"),
        ("failed_partial", "compensating"),
        ("compensating", "settled"),
    ])
    def test_legal_transitions(self, source, target):
        assert transition(source, target).value == target

    @pytest.mark.parametrize("source,target", [
        ("indeterminate", "dispatched"),      # 未知结果不允许直接重发
        ("settled", "dispatched"),            # 已结算不能重跑
        ("proposed", "settled"),              # 意图不能直接当结论
    ])
    def test_illegal_transitions(self, source, target):
        with pytest.raises(IllegalToolCallTransition):
            transition(source, target)

    def test_idempotency_key_is_stable_and_args_sensitive(self):
        first = idempotency_key("compassrag", {"query": "x", "k": 1}, scope="s1/S1")
        reordered = idempotency_key("compassrag", {"k": 1, "query": "x"}, scope="s1/S1")
        changed = idempotency_key("compassrag", {"query": "y", "k": 1}, scope="s1/S1")
        assert first == reordered, "同语义不同键序必须同 key"
        assert first != changed, "语义变了才是新操作"
        assert args_fingerprint({"b": 2, "a": 1}) == args_fingerprint({"a": 1, "b": 2})

    def test_observation_decision_three_way(self):
        applied = decide_from_observation({"state": "done"}, expected={"state": "done"})
        assert applied.status is ToolCallStatus.RECONCILED
        assert applied.outcome is Outcome.APPLIED

        unknown = decide_from_observation(None)
        assert unknown.status is ToolCallStatus.INDETERMINATE
        assert unknown.outcome is None

        not_applied = decide_from_observation({"state": "old"}, fingerprint_matches=False)
        assert not_applied.outcome is Outcome.NOT_APPLIED


class TestDedupe:
    def test_same_call_is_stored_once(self, repo):
        first = repo.propose_tool_call("s1", tool_name="compassrag", args={"query": "x"},
                                       step_id="retrieve")
        second = repo.propose_tool_call("s1", tool_name="compassrag", args={"query": "x"},
                                        step_id="retrieve")
        assert first["created"] is True
        assert second["created"] is False
        assert first["tool_call"]["tool_call_id"] == second["tool_call"]["tool_call_id"]
        assert len(repo.tool_calls("s1")) == 1

    def test_dispatch_is_atomic(self, repo):
        call = repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1})["tool_call"]
        assert repo.dispatch_tool_call("s1", call["tool_call_id"]) is True
        assert repo.dispatch_tool_call("s1", call["tool_call_id"]) is False

    def test_duplicate_response_is_idempotent(self, repo):
        call = repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1})["tool_call"]
        repo.dispatch_tool_call("s1", call["tool_call_id"])
        first = repo.settle_tool_call("s1", call["tool_call_id"], status="settled",
                                      outcome="applied", result={"n": 1})
        second = repo.settle_tool_call("s1", call["tool_call_id"], status="settled",
                                       outcome="applied")
        assert second["status"] == first["status"]
        assert second["result"] == {"n": 1}, "重复响应不能清掉已有结果"


class TestDispatcher:
    def _dispatcher(self, repo, **spec_kwargs):
        spec = ToolSpec(name="compassrag", effect_class=EffectClass.READ_ONLY, **spec_kwargs)
        return ToolDispatcher(repo, "s1", spec, step_id="retrieve")

    def test_success_settles_applied(self, repo):
        outcome = self._dispatcher(repo).invoke(lambda: {"refs": ["c1"]}, {"query": "x"})
        assert outcome.ok is True
        assert outcome.outcome == Outcome.APPLIED.value
        assert repo.tool_calls("s1")[0]["status"] == "settled"

    def test_crash_before_send_is_retried_safely(self, repo):
        """只落了意图（没派发）就崩：重进可以安全派发一次。"""
        repo.propose_tool_call("s1", tool_name="compassrag", args={"query": "x"},
                               step_id="retrieve")
        sent = []
        outcome = self._dispatcher(repo).invoke(lambda: sent.append(1) or {"ok": True},
                                                {"query": "x"})
        assert outcome.status is ToolCallStatus.SETTLED
        assert len(sent) == 1
        assert len(repo.tool_calls("s1")) == 1

    def test_response_lost_after_dispatch_is_never_resent(self, repo):
        """派发后进程挂了：结果可能已生效，只能对账，绝不重发。"""
        call = repo.propose_tool_call("s1", tool_name="compassrag", args={"query": "x"},
                                      step_id="retrieve")["tool_call"]
        repo.dispatch_tool_call("s1", call["tool_call_id"])
        sent = []
        outcome = self._dispatcher(repo).invoke(lambda: sent.append(1) or {"ok": True},
                                                {"query": "x"})
        assert outcome.needs_human is True
        assert outcome.status is ToolCallStatus.INDETERMINATE
        assert outcome.error["kind"] == "unknown_commit"
        assert sent == [], "结果未知时绝不能重发"

    def test_timeout_becomes_indeterminate(self, repo):
        dispatcher = self._dispatcher(repo)
        outcome = dispatcher.invoke(
            lambda: (_ for _ in ()).throw(ResearchToolError("timeout", "超时")),
            {"query": "y"})
        assert outcome.status is ToolCallStatus.INDETERMINATE
        assert outcome.outcome == Outcome.UNKNOWABLE.value
        assert outcome.needs_human

    def test_determinate_error_is_retryable(self, repo):
        dispatcher = self._dispatcher(repo)
        outcome = dispatcher.invoke(
            lambda: (_ for _ in ()).throw(ResearchToolError("invalid_request", "bad")),
            {"query": "z"})
        assert outcome.status is ToolCallStatus.FAILED_DETERMINATE
        assert outcome.outcome == Outcome.NOT_APPLIED.value
        assert outcome.retryable
        # 确定没生效 → 允许再派发一次（同一个幂等键、同一行）
        again = dispatcher.invoke(lambda: {"ok": True}, {"query": "z"})
        assert again.ok is True
        assert repo.tool_calls("s1")[0]["retry_count"] == 1

    def test_reconcile_with_lookup_recovers_result(self, repo):
        dispatcher = self._dispatcher(repo, supports_idempotency=True, supports_lookup=True)
        outcome = dispatcher.invoke(
            lambda: (_ for _ in ()).throw(ResearchToolError("upstream_error", "断线")),
            {"query": "x"})
        assert outcome.needs_human

        fixed = dispatcher.reconcile(
            outcome.tool_call_id,
            lookup=lambda key: {"answer": "1879"},
        )
        assert fixed.status is ToolCallStatus.RECONCILED
        assert fixed.outcome == Outcome.APPLIED.value
        assert fixed.result == {"answer": "1879"}

    def test_reconcile_without_evidence_stays_unknowable(self, repo):
        dispatcher = self._dispatcher(repo)
        outcome = dispatcher.invoke(
            lambda: (_ for _ in ()).throw(ResearchToolError("timeout", "超时")),
            {"query": "x"})
        after = dispatcher.reconcile(outcome.tool_call_id, observe=lambda: None)
        assert after.status is ToolCallStatus.INDETERMINATE
        assert after.outcome == Outcome.UNKNOWABLE.value
        record = repo.get_tool_call("s1", outcome.tool_call_id)
        assert record["reconcile_attempts"] == 1

    def test_reconcile_with_matching_observation_marks_applied(self, repo):
        dispatcher = self._dispatcher(repo)
        outcome = dispatcher.invoke(
            lambda: (_ for _ in ()).throw(ResearchToolError("timeout", "超时")),
            {"query": "x"})
        after = dispatcher.reconcile(outcome.tool_call_id,
                                     observe=lambda: {"state": "done"},
                                     expected={"state": "done"})
        assert after.status is ToolCallStatus.RECONCILED
        assert after.outcome == Outcome.APPLIED.value

    def test_compensation_is_a_new_call(self, repo):
        original = repo.propose_tool_call("s1", tool_name="write", args={"n": 1})["tool_call"]
        repo.dispatch_tool_call("s1", original["tool_call_id"])
        repo.settle_tool_call("s1", original["tool_call_id"], status="failed_partial",
                              outcome="partially_applied")
        compensation = repo.compensate_tool_call(
            "s1", original["tool_call_id"], tool_name="write", args={"n": -1},
            reason="撤销部分写入")
        assert compensation["tool_call_id"] != original["tool_call_id"]
        assert compensation["compensation_for"] == original["tool_call_id"]
        # 原调用历史仍在，没有被删改
        assert repo.get_tool_call("s1", original["tool_call_id"])["status"] == "failed_partial"
        assert len(repo.tool_calls("s1")) == 2


class TestStepIntegration:
    """DAG 的 retrieve 步走 dispatcher：成功留记录，超时冻结而不是重发。"""

    AUDIT = json.dumps({"verdict": "ok", "issues": "", "refs": ["c1"]})

    class _LLM:
        def __init__(self, audit):
            self.audit = audit

        def __call__(self, prompt):
            if "revising your draft" in prompt:
                return "修订稿"
            return self.audit if "AUDITOR" in prompt else "草稿"

    class _Tool:
        def __init__(self, error=None):
            self.error = error
            self.calls = 0

        def research(self, request):
            self.calls += 1
            if self.error is not None:
                raise self.error
            from atlas.tools.protocol import EvidenceItem, ResearchResult

            return ResearchResult(
                answer="1879 年",
                evidence=[EvidenceItem(source="Einstein", chunk_id="c1",
                                       quote="1879 年出生于乌尔姆。", score=0.9,
                                       retriever="bm25", rank=1)],
                confidence=0.8, retrieval_rounds=1, stop_reason="answered",
                trace_id="ct-1",
            )

    def _client(self, db, tool):
        return TestClient(create_app(
            GraphRepository(db), research_tool=tool,
            orchestrate_llm=self._LLM(self.AUDIT),
            context_token_counter=lambda text: (len(text) + 3) // 4,
        ))

    def _run(self, client):
        client.post("/tasks", json={"session_id": "s1", "goal": "q"})
        client.post("/tasks/s1/plans", json={"problem": "爱因斯坦出生于哪一年？",
                                             "activate": True})
        return client.post("/tasks/s1/plans/P1/advance", json={}).json()

    def test_successful_retrieve_records_tool_call(self, db):
        tool = self._Tool()
        client = self._client(db, tool)
        body = self._run(client)
        assert body["complete"] is True
        calls = GraphRepository(db).tool_calls("s1")
        assert [item["status"] for item in calls] == ["settled"]
        assert calls[0]["step_id"] == "retrieve"
        assert calls[0]["outcome"] == "applied"
        assert calls[0]["idempotency_key"].startswith("compassrag:s1/retrieve:")

    def test_timeout_freezes_the_step_without_resend(self, db):
        tool = self._Tool(error=ResearchToolError("timeout", "超时"))
        client = self._client(db, tool)
        body = self._run(client)
        assert body["needs_human"] == ["retrieve"]
        assert body["state"] == "paused"
        calls = GraphRepository(db).tool_calls("s1")
        assert calls[0]["status"] == "indeterminate"

        # 再推进一次：工具不应被再次调用（结果未知不重发）
        client.post("/tasks/s1/plans/P1/advance", json={})
        assert tool.calls == 1


class TestToolCallAPI:
    """对账必须能从外面触发：未知结果不能只靠后台线程收拾。"""

    def _client(self, db):
        return TestClient(create_app(
            GraphRepository(db), research_tool=None,
            orchestrate_llm=lambda prompt: "{}",
            context_token_counter=lambda text: (len(text) + 3) // 4,
        ))

    def _indeterminate_call(self, repo):
        repo.propose_tool_call("s1", tool_name="compassrag", args={"query": "x"},
                               step_id="retrieve")
        call = repo.tool_calls("s1")[0]
        repo.dispatch_tool_call("s1", call["tool_call_id"])
        repo.settle_tool_call("s1", call["tool_call_id"], status="indeterminate",
                              outcome="unknowable",
                              error={"kind": "unknown_commit", "message": "断线"})
        return call["tool_call_id"]

    def test_list_shows_four_state_fields(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        call_id = self._indeterminate_call(repo)
        client = self._client(db)
        rows = client.get("/tasks/s1/tool-calls").json()["tool_calls"]
        assert len(rows) == 1
        assert rows[0]["tool_call_id"] == call_id
        assert rows[0]["status"] == "indeterminate"
        assert rows[0]["outcome"] == "unknowable"
        assert rows[0]["idempotency_key"].startswith("compassrag:s1/retrieve:")

    def test_reconcile_with_explicit_outcome(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        call_id = self._indeterminate_call(repo)
        client = self._client(db)
        body = client.post(f"/tasks/s1/tool-calls/{call_id}/reconcile",
                           json={"outcome": "applied", "detail": "人工确认已写入"}).json()
        assert body["status"] == "reconciled"
        assert body["outcome"] == "applied"

    def test_reconcile_from_observation_can_stay_unknown(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        call_id = self._indeterminate_call(repo)
        client = self._client(db)
        body = client.post(f"/tasks/s1/tool-calls/{call_id}/reconcile",
                           json={"observed": None}).json()
        assert body["status"] == "indeterminate"
        assert body["outcome"] == "unknowable"
        assert body["reconcile_attempts"] == 1

    def test_settled_call_cannot_be_reconciled(self, db):
        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        repo.propose_tool_call("s1", tool_name="compassrag", args={"q": 1})
        call = repo.tool_calls("s1")[0]
        repo.dispatch_tool_call("s1", call["tool_call_id"])
        repo.settle_tool_call("s1", call["tool_call_id"], status="settled",
                              outcome="applied")
        client = self._client(db)
        response = client.post(f"/tasks/s1/tool-calls/{call['tool_call_id']}/reconcile",
                               json={"outcome": "applied"})
        assert response.status_code == 409
