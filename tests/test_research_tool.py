"""阶段 A：ResearchTool 协议、CompassRAG 适配器与工具接入编排。

对应计划阶段 A 的任务与验收：
- ResearchRequest / EvidenceItem / ResearchResult / ResearchTool protocol；
- 本地适配器与 HTTP 适配器语义一致；
- 工具错误归为 timeout / invalid_request / no_evidence / budget_exhausted / upstream_error；
- 每次调用有 trace_id 与幂等键；
- 结果写入 Fact 并保留 evidence provenance；
- 验收：fake 工具跑通 Planner→Worker→Auditor；工具失败不破坏已有状态；同一幂等键不重复写入。
"""

import json
import socket
import urllib.error

import pytest

from atlas.orchestration import Orchestrator, TaskState, TaskStateMachine
from atlas.tools import (
    ERROR_KINDS,
    EvidenceItem,
    HttpCompassRAGTool,
    LocalResearchTool,
    ResearchRequest,
    ResearchResult,
    ResearchToolError,
    build_idempotency_key,
)
from atlas.tools.compassrag import describe_failure


class TestProtocol:
    def test_request_requires_core_fields(self):
        with pytest.raises(ValueError):
            ResearchRequest(session_id="s", subtask_id="S1", query="  ")
        with pytest.raises(ValueError):
            ResearchRequest(session_id="s", subtask_id="S1", query="q", budget_tokens=0)
        with pytest.raises(ValueError):
            ResearchRequest(session_id="s", subtask_id="S1", query="q", max_rounds=0)

    def test_request_fills_trace_and_idempotency_ids(self):
        request = ResearchRequest(session_id="s1", subtask_id="S3", query="查一下 Tate 界")
        assert request.trace_id.startswith("trace-")
        assert request.idempotency_key == build_idempotency_key("s1", "S3")
        assert request.to_dict()["subtask_id"] == "S3"

    def test_result_from_dict_normalizes_evidence_and_clamps_confidence(self):
        result = ResearchResult.from_dict(
            {
                "answer": "结论",
                "confidence": 1.7,
                "evidence": [{"source": "paper:1", "chunk_id": "c1", "quote": "q", "score": "0.9"}],
                "retrieval_rounds": 2,
                "stop_reason": "evidence_sufficient",
                "trace_id": "trace-x",
            }
        )
        assert result.confidence == 1.0
        assert result.evidence[0].source == "paper:1"
        assert result.evidence[0].score == 0.9
        assert result.stop_reason == "evidence_sufficient"
        assert result.usable

    def test_evidence_without_source_is_rejected(self):
        with pytest.raises(ValueError):
            EvidenceItem.from_dict({"chunk_id": "c1"})

    def test_error_kinds_are_exactly_five(self):
        assert ERROR_KINDS == (
            "timeout",
            "invalid_request",
            "no_evidence",
            "budget_exhausted",
            "upstream_error",
        )
        with pytest.raises(ValueError):
            ResearchToolError("weird")


class TestLocalAdapter:
    def test_wraps_callable_returning_dict(self):
        tool = LocalResearchTool(lambda req: {"answer": "A", "evidence": [{"source": "s"}]})
        result = tool.research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert isinstance(result, ResearchResult)
        assert result.evidence[0].source == "s"
        assert tool.calls == 1

    def test_timeout_is_classified(self):
        def boom(_request):
            raise socket.timeout("timed out")

        with pytest.raises(ResearchToolError) as excinfo:
            LocalResearchTool(boom).research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert excinfo.value.kind == "timeout"

    def test_unknown_exception_is_upstream_error(self):
        def boom(_request):
            raise RuntimeError("检索挂了")

        with pytest.raises(ResearchToolError) as excinfo:
            LocalResearchTool(boom).research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert excinfo.value.kind == "upstream_error"

    def test_malformed_payload_is_invalid_request(self):
        with pytest.raises(ResearchToolError) as excinfo:
            LocalResearchTool(lambda req: {"evidence": "不是数组"}).research(
                ResearchRequest(session_id="s", subtask_id="S1", query="q")
            )
        assert excinfo.value.kind == "invalid_request"

    def test_describe_failure_is_trace_friendly(self):
        failure = describe_failure(ResearchToolError("timeout", "slow", trace_id="trace-1"))
        assert failure == {"kind": "timeout", "message": "slow", "trace_id": "trace-1"}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.seen = []

    def __call__(self, request, timeout=None):
        self.seen.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload)


class TestHttpAdapter:
    def test_success_parses_and_carries_ids(self):
        opener = FakeOpener(
            {"answer": "结论", "evidence": [{"source": "kb:1"}], "retrieval_rounds": 2,
             "stop_reason": "evidence_sufficient", "confidence": 0.8}
        )
        tool = HttpCompassRAGTool("http://127.0.0.1:9000/", opener=opener)
        request = ResearchRequest(session_id="s", subtask_id="S1", query="q")
        result = tool.research(request)
        assert result.answer == "结论"
        assert result.trace_id == request.trace_id
        sent = opener.seen[0][0]
        assert sent.headers["X-idempotency-key"] == request.idempotency_key
        assert sent.full_url == "http://127.0.0.1:9000/research"

    @pytest.mark.parametrize(
        "status,kind",
        [(400, "invalid_request"), (422, "invalid_request"), (408, "timeout"),
         (504, "timeout"), (429, "budget_exhausted"), (404, "no_evidence"),
         (500, "upstream_error")],
    )
    def test_http_status_maps_to_error_kind(self, status, kind):
        opener = FakeOpener(error=urllib.error.HTTPError("http://x/research", status, "err", {}, None))
        tool = HttpCompassRAGTool("http://x", opener=opener)
        with pytest.raises(ResearchToolError) as excinfo:
            tool.research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert excinfo.value.kind == kind

    def test_connection_error_maps_to_upstream_error(self):
        opener = FakeOpener(error=urllib.error.URLError("connection refused"))
        tool = HttpCompassRAGTool("http://x", opener=opener)
        with pytest.raises(ResearchToolError) as excinfo:
            tool.research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert excinfo.value.kind == "upstream_error"

    def test_timeout_maps_to_timeout(self):
        opener = FakeOpener(error=urllib.error.URLError(socket.timeout("timed out")))
        tool = HttpCompassRAGTool("http://x", opener=opener)
        with pytest.raises(ResearchToolError) as excinfo:
            tool.research(ResearchRequest(session_id="s", subtask_id="S1", query="q"))
        assert excinfo.value.kind == "timeout"


class RecordingGraph:
    def __init__(self):
        self.notes = []

    def state_view(self, role):
        return f"（{role} 的状态视图）"

    def role_note(self, role, kind, content, reason="", evidence=""):
        self.notes.append(
            {"role": role, "kind": kind, "content": content, "reason": reason, "evidence": evidence}
        )
        return {"node_id": f"{role}-{kind}", "kind": kind, "actor": role}

    def research_notes(self):
        return [n for n in self.notes if n["role"] == "research"]


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


PLAN = json.dumps({"route": "用鞅方法证明上界", "risks": "变量相关性", "reason": "题设依赖"})
AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})
PROBLEM = "题目：给出 L(h^t,w^t) 的上界"


def research_payload(answer="L ≤ 2τσ²", source="paper:2312.05134", rounds=2):
    return {
        "answer": answer,
        "evidence": [{"source": source, "chunk_id": "c1", "quote": "Theorem 3", "score": 0.91}],
        "confidence": 0.84,
        "retrieval_rounds": rounds,
        "stop_reason": "evidence_sufficient",
        "trace_id": "trace-from-tool",
    }


class TestToolInOrchestration:
    def test_full_flow_with_fake_tool_writes_fact_with_provenance(self):
        graph = RecordingGraph()
        tool = LocalResearchTool(lambda req: research_payload())
        machine = TaskStateMachine()
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿解答", AUDIT_OK]), graph, machine=machine, research_tool=tool
        )
        answer, trace = orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")

        assert answer == "草稿解答"
        assert machine.state == TaskState.COMPLETED
        assert tool.calls == 1
        notes = graph.research_notes()
        assert len(notes) == 1
        evidence = json.loads(notes[0]["evidence"])
        assert evidence[0]["source"] == "paper:2312.05134"
        assert trace.research_rounds == 2
        assert trace.research_stop_reason == "evidence_sufficient"
        assert trace.research_trace_id
        assert trace.fact_ids == ["research-fact"]

    def test_tool_failure_keeps_state_and_task_completes(self):
        def boom(_request):
            raise socket.timeout("timed out")

        graph = RecordingGraph()
        machine = TaskStateMachine()
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿解答", AUDIT_OK]),
            graph,
            machine=machine,
            research_tool=LocalResearchTool(boom),
        )
        answer, trace = orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")
        assert answer == "草稿解答"
        assert machine.state == TaskState.COMPLETED
        assert graph.research_notes() == []          # 失败不写图
        assert trace.research_error["kind"] == "timeout"

    def test_same_idempotency_key_does_not_duplicate_writes(self):
        graph = RecordingGraph()
        tool = LocalResearchTool(lambda req: research_payload())
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿一", AUDIT_OK, PLAN, "草稿二", AUDIT_OK]),
            graph,
            research_tool=tool,
        )
        orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")
        _, second = orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")

        assert tool.calls == 1                        # 幂等键命中缓存，不重复调工具
        assert len(graph.research_notes()) == 1       # 也不重复写 Fact
        assert second.research_reused is True

    def test_no_answer_is_treated_as_no_evidence(self):
        graph = RecordingGraph()
        tool = LocalResearchTool(lambda req: research_payload(answer="", rounds=3))
        machine = TaskStateMachine()
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿解答", AUDIT_OK]), graph, machine=machine, research_tool=tool
        )
        _, trace = orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")
        assert graph.research_notes() == []
        assert trace.research_error["kind"] == "no_evidence"
        assert machine.state == TaskState.COMPLETED

    def test_without_tool_behaves_as_before(self):
        graph = RecordingGraph()
        machine = TaskStateMachine()
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿解答", AUDIT_OK]), graph, machine=machine
        )
        answer, trace = orchestrator.solve(PROBLEM)
        assert answer == "草稿解答"
        assert trace.research_error is None
        assert graph.research_notes() == []


class TestTransitionCheckpoints:
    def test_every_successful_transition_emits_a_checkpoint(self):
        snapshots = []
        machine = TaskStateMachine(checkpoint_sink=snapshots.append)
        machine.apply("plan_ready", reason="路线已定")
        machine.apply("work_done", reason="草稿完成")
        machine.apply("audit_pass", reason="通过")
        assert [s["state"] for s in snapshots] == ["executing", "auditing", "completed"]
        assert snapshots[-1]["history_len"] == 3
