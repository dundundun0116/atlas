"""Phase 2 收尾：研究流程作为显式 Steps（`plan_dag_v1`）。

三组：
1. 计划生成 —— 确定性的 5 步 DAG，能过七道闸；
2. 执行器 —— 每步产出结构化 output，工具/模型失败是"确定失败"而不是 unknown；
3. API —— 提交 / 激活 / 推进，以及"计划推进结果同步到任务生命周期"。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.planning.executors import ResearchStepExecutor
from atlas.planning.research_plan import RESEARCH_PLAN_WORKFLOW, allowed_tools, build_research_plan
from atlas.planning.validator import validate_plan
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult
from atlas.tools.protocol import ResearchToolError

AUDIT_OK = json.dumps({"verdict": "ok", "issues": "", "refs": ["c1"]})
AUDIT_REVISE = json.dumps({"verdict": "revise", "issues": "没有引用证据", "refs": ["c1"]})

PROBLEM = "爱因斯坦出生于哪一年？"


class ScriptedLLM:
    def __init__(self, audit=AUDIT_OK, draft="爱因斯坦出生于 1879 年。"):
        self.audit = audit
        self.draft = draft
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if "revising your draft" in prompt:  # 先判修订稿：它的提示词里也提到 AUDITOR
            return "修订：爱因斯坦出生于 1879 年（乌尔姆，据传记 c1）。"
        if "AUDITOR" in prompt:
            return self.audit
        return self.draft


class FakeTool:
    def __init__(self, error=None):
        self.error = error

    def research(self, request):
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
    database = open_database(tmp_path / "plan_api.db")
    yield database
    database.close()


class TestResearchPlan:
    def test_plan_is_deterministic_and_valid(self):
        first = build_research_plan(PROBLEM)
        second = build_research_plan(PROBLEM)
        assert first.to_dict() == second.to_dict()
        assert first.step_ids == ["retrieve", "draft", "verify", "repair", "final_verify"]

        report = validate_plan(first, allowed_tools=allowed_tools())
        assert report.ok, report.error_codes()
        assert report.topological_order == first.step_ids
        assert report.human_required == []

    def test_without_research_has_no_tool_step(self):
        plan = build_research_plan(PROBLEM, with_research=False)
        assert "retrieve" not in plan.step_ids
        assert validate_plan(plan, allowed_tools=[]).ok

    def test_without_repair_links_draft_straight_to_final(self):
        plan = build_research_plan(PROBLEM, with_repair=False)
        assert "repair" not in plan.step_ids
        assert ("draft", "final_verify") in [
            (item.from_step_id, item.to_step_id) for item in plan.dependencies
        ]

    def test_data_bindings_reference_real_outputs_and_inputs(self):
        plan = build_research_plan(PROBLEM)
        data_edges = [item for item in plan.dependencies if item.kind.value == "data"]
        assert data_edges
        for dependency in data_edges:
            upstream = plan.step(dependency.from_step_id)
            downstream = plan.step(dependency.to_step_id)
            assert upstream.output_named(dependency.binding["output_name"]) is not None
            assert downstream.input_named(dependency.binding["input_name"]) is not None

    def test_empty_problem_is_refused(self):
        with pytest.raises(ValueError):
            build_research_plan("   ")


class TestStepExecutor:
    def _executor(self, *, tool=None, audit=AUDIT_OK, inputs=None):
        return ResearchStepExecutor(
            ScriptedLLM(audit=audit), problem=PROBLEM,
            research_tool=tool or FakeTool(), session_id="s1",
        )

    def _step(self, step_id):
        return build_research_plan(PROBLEM).step(step_id)

    def test_retrieve_returns_refs(self):
        outcome = self._executor()(self._step("retrieve"), {"inputs": {}})
        assert outcome.status == "succeeded"
        assert outcome.output["refs"] and outcome.output["refs"][0]["chunk_id"] == "c1"

    def test_retrieve_without_tool_fails_deterministically(self):
        executor = ResearchStepExecutor(ScriptedLLM(), problem=PROBLEM, research_tool=None)
        outcome = executor(self._step("retrieve"), {"inputs": {}})
        assert outcome.status == "failed"
        assert outcome.error["kind"] == "tool_unavailable"

    def test_retrieve_tool_error_keeps_kind(self):
        tool = FakeTool(error=ResearchToolError("timeout", "超时", trace_id="t1"))
        outcome = self._executor(tool=tool)(self._step("retrieve"), {"inputs": {}})
        assert outcome.status == "failed"
        assert outcome.error["kind"] == "timeout"
        assert outcome.error["trace_id"] == "t1"

    def test_draft_uses_evidence_from_binding(self):
        executor = self._executor()
        outcome = executor(self._step("draft"), {"inputs": {"evidence": [
            {"source": "Einstein", "chunk_id": "c1", "quote": "1879 年出生于乌尔姆。"}
        ]}})
        assert outcome.status == "succeeded"
        assert outcome.output["draft"]

    def test_missing_required_input_fails_before_calling_model(self):
        executor = self._executor()
        outcome = executor(self._step("verify"), {"inputs": {}})
        assert outcome.status == "failed"
        assert outcome.error["kind"] == "missing_inputs"

    def test_verify_normalises_verdict(self):
        outcome = self._executor()(self._step("verify"),
                                   {"inputs": {"draft": "草稿"}})
        assert outcome.output["verdict"] == "pass"
        assert outcome.output["raw_verdict"] == "ok"

    def test_repair_is_noop_without_issues(self):
        outcome = self._executor()(self._step("repair"),
                                   {"inputs": {"draft": "草稿", "issues": ""}})
        assert outcome.output == {"draft": "草稿", "revised": False}

    def test_repair_rewrites_when_issues_given(self):
        outcome = self._executor()(self._step("repair"),
                                   {"inputs": {"draft": "草稿", "issues": "没引用证据"}})
        assert outcome.output["revised"] is True
        assert outcome.output["draft"].startswith("修订")

    def test_final_verify_marks_approved_only_on_pass(self):
        ok = self._executor()(self._step("final_verify"), {"inputs": {"draft": "草稿"}})
        assert ok.output["approved"] is True
        bad = self._executor(audit=AUDIT_REVISE)(
            self._step("final_verify"), {"inputs": {"draft": "草稿"}})
        assert bad.output["approved"] is False


def _client(db, *, llm=None, tool="default", **kwargs):
    if tool == "default":
        tool = FakeTool()
    return TestClient(create_app(
        GraphRepository(db), research_tool=tool,
        orchestrate_llm=llm or ScriptedLLM(),
        context_token_counter=lambda text: (len(text) + 3) // 4,
        **kwargs,
    ))


def _create(client, session="s1"):
    assert client.post("/tasks", json={"session_id": session, "goal": PROBLEM}).status_code == 200


class TestPlanAPI:
    def test_submit_builds_and_validates_plan(self, db):
        client = _client(db)
        _create(client)
        body = client.post("/tasks/s1/plans", json={"problem": PROBLEM}).json()
        assert body["plan"]["status"] == "validated"
        assert [step["step_id"] for step in body["steps"]] == [
            "retrieve", "draft", "verify", "repair", "final_verify",
        ]
        assert body["plan"]["validation"]["ok"] is True
        # 7 条：retrieve→draft / retrieve→verify / retrieve→final_verify（证据三处都要）
        # + draft→verify / draft→repair / verify→repair / repair→final_verify
        assert len(body["dependencies"]) == 7

    def test_submit_rejects_invalid_plan_json(self, db):
        client = _client(db)
        _create(client)
        response = client.post("/tasks/s1/plans", json={"plan": {"steps": [
            {"step_id": "A", "kind": "llm"},
            {"step_id": "B", "kind": "llm"},
        ], "dependencies": [
            {"from_step_id": "A", "to_step_id": "B"},
            {"from_step_id": "B", "to_step_id": "A"},
        ]}})
        body = response.json()
        assert response.status_code == 200
        assert body["plan"]["status"] == "rejected"
        assert "CYCLE" in [item["code"] for item in body["plan"]["validation"]["issues"]]

    def test_submit_requires_plan_or_problem(self, db):
        client = _client(db)
        _create(client)
        response = client.post("/tasks/s1/plans", json={})
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request"

    def test_submit_with_research_requires_tool(self, db):
        client = _client(db, tool=None)
        _create(client)
        response = client.post("/tasks/s1/plans", json={"problem": PROBLEM})
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "unrecoverable"

    def test_rejected_plan_cannot_be_activated(self, db):
        client = _client(db)
        _create(client)
        cyclic = {
            "steps": [{"step_id": "A"}, {"step_id": "B"}],
            "dependencies": [
                {"from_step_id": "A", "to_step_id": "B"},
                {"from_step_id": "B", "to_step_id": "A"},
            ],
        }
        body = client.post("/tasks/s1/plans", json={"plan": cyclic}).json()
        assert body["plan"]["status"] == "rejected"
        response = client.post("/tasks/s1/plans/P1/activate", json={})
        assert response.status_code == 409

    def test_advance_requires_active_plan(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM})
        response = client.post("/tasks/s1/plans/P1/advance", json={})
        assert response.status_code == 409
        assert "先激活" in response.json()["error"]["message"]

    def test_activate_switches_workflow_version(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM})
        assert client.get("/tasks/s1").json()["workflow_version"] == "legacy_research_v1"
        client.post("/tasks/s1/plans/P1/activate", json={})
        assert client.get("/tasks/s1").json()["workflow_version"] == RESEARCH_PLAN_WORKFLOW

    def test_advance_runs_dag_to_completion(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM, "activate": True})
        body = client.post("/tasks/s1/plans/P1/advance", json={}).json()
        assert body["complete"] is True
        assert body["executed"] == ["retrieve", "draft", "verify", "repair", "final_verify"]
        assert {step["status"] for step in body["steps"]} == {"done"}
        # 任务生命周期同步到终态，不悬在 planning
        assert body["state"] == "completed"
        assert body["workflow_version"] == RESEARCH_PLAN_WORKFLOW

    def test_second_advance_does_not_rerun(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM, "activate": True})
        client.post("/tasks/s1/plans/P1/advance", json={})
        again = client.post("/tasks/s1/plans/P1/advance", json={}).json()
        assert again["executed"] == []

    def test_failed_final_verify_stops_the_run(self, db):
        client = _client(db, llm=ScriptedLLM(audit=AUDIT_REVISE))
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM, "activate": True})
        body = client.post("/tasks/s1/plans/P1/advance", json={}).json()
        assert body["complete"] is False
        assert "final_verify" in body["failed"]
        assert body["state"] == "failed"

    def test_advance_recovers_only_in_flight_step(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM, "activate": True})
        repo = GraphRepository(db)
        repo.claim_step("s1", "P1", "retrieve")  # 模拟"派发后就崩了"
        body = client.post("/tasks/s1/plans/P1/advance", json={}).json()
        assert body["recovered_in_flight"] == ["retrieve"]
        assert body["executed"][0] == "retrieve"
        assert body["complete"] is True

    def test_unknown_plan_is_404(self, db):
        client = _client(db)
        _create(client)
        assert client.get("/tasks/s1/plans/P9").status_code == 404

    def test_plan_detail_exposes_attempts_and_stale(self, db):
        client = _client(db)
        _create(client)
        client.post("/tasks/s1/plans", json={"problem": PROBLEM, "activate": True})
        client.post("/tasks/s1/plans/P1/advance", json={})
        detail = client.get("/tasks/s1/plans/P1").json()
        assert all(step["attempt_count"] == 1 for step in detail["steps"])

        repo = GraphRepository(db)
        stale = repo.mark_downstream_stale("s1", "P1", ["draft"])
        assert "verify" in stale
        after = client.get("/tasks/s1/plans/P1").json()
        statuses = {step["step_id"]: step["status"] for step in after["steps"]}
        assert statuses["verify"] == "stale"
        assert statuses["draft"] == "done"


class TestVerifierSeesEvidence:
    """核验者必须拿到草稿依据的证据，否则它只能判"这句话没出处"。

    这条不是洁癖：真模型一跑就暴露了——verify 只绑 draft 时，auditor 会把
    **引用正确证据的草稿**判成无出处，再让修订步把正确答案删掉。
    """

    @staticmethod
    def _executor(llm):
        from atlas.planning.executors import ResearchStepExecutor

        return ResearchStepExecutor(llm, problem="某器件的标定年份是哪一年？")

    def test_verify_prompt_contains_the_evidence(self):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps({"verdict": "ok", "issues": "", "refs": []})

        plan = build_research_plan("某器件的标定年份是哪一年？")
        executor = self._executor(llm)
        outcome = executor._do_verify(plan.step("verify"), {
            "draft": "答案是 1975 年。",
            "evidence": [{"source": "内部技术档案", "chunk_id": "c1",
                          "quote": "档案原文：某器件的标定年份为 ANSWER:1975。"}],
        })
        assert outcome.status == "succeeded"
        assert "ANSWER:1975" in prompts[0], "证据必须出现在核验者的上下文里"

    def test_final_verify_prompt_contains_the_evidence(self):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps({"verdict": "ok", "issues": "", "refs": []})

        plan = build_research_plan("某器件的标定年份是哪一年？")
        executor = self._executor(llm)
        executor._do_final_verify(plan.step("final_verify"), {
            "draft": "答案是 1975 年。",
            "evidence": [{"source": "内部技术档案", "chunk_id": "c1",
                          "quote": "档案原文：某器件的标定年份为 ANSWER:1975。"}],
        })
        assert "ANSWER:1975" in prompts[0]

    def test_plan_binds_evidence_into_both_verifiers(self):
        plan = build_research_plan("问题")
        thens = {(item.from_step_id, item.to_step_id, item.binding["input_name"])
                 for item in plan.dependencies if item.binding}
        assert ("retrieve", "verify", "evidence") in thens
        assert ("retrieve", "final_verify", "evidence") in thens
