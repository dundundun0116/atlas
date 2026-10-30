"""合并验收：Atlas 通过 ResearchTool 驱动**同仓**的 CompassRAG（进程内真实桥接）。

和 `test_research_tool.py` 里的函数替身 / HTTP 假 opener 不同，这里用的是 CompassRAG
自己的 `Agent`（LangGraph 控制流 + 假 LLM / 假检索器），因此一条测试同时锁住三件事：

1. CompassRAG 对外契约（ResearchRequest/ResearchResult）没有漂移；
2. `CompassRAGAgentTool` 的双向映射（请求、证据 retriever/rank、failure → 五类错误）；
3. Atlas 编排在拿到真实证据后写 Fact、保留 provenance 与 trace_id。
"""

import json

import pytest

from atlas.orchestration import Orchestrator, TaskStateMachine
from atlas.tools import (
    ERROR_KINDS,
    CompassRAGAgentTool,
    ResearchRequest,
    ResearchToolError,
)
from atlas.tools.compassrag import _COMPASS_FAILURE_TO_ATLAS
from compassrag.agent import Agent, AgentConfig
from compassrag.llm.client import LLMResponse

STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
    "c3": {"chunk_id": "c3", "title": "Gamma", "text": "Gamma text."},
}


class FakeTool:
    def __init__(self, ranking):
        self.ranking = ranking
        self.queries = []

    def search(self, query, k=10):
        self.queries.append(query)
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class FakeLLM:
    """CompassRAG 的 LLM 最小替身：按队列返回文本，用完回落到默认答案。"""

    def __init__(self, text="Paris"):
        self.text = text
        self.queue = []
        self.calls = []

    def script(self, texts):
        self.queue = list(texts)
        return self

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"messages": messages, "tag": tag, "max_tokens": max_tokens})
        text = self.queue.pop(0) if self.queue else self.text
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0,
                           finish_reason="stop" if text.strip() else "length")


def _agent(ranking=("c1", "c2", "c3"), *, top_k=2, text="Paris"):
    return Agent(FakeLLM(text), {"bm25": FakeTool(ranking)}, STORE, AgentConfig(top_k=top_k))


def _request(**kwargs):
    payload = dict(session_id="s1", subtask_id="S1", query="Where is Alpha?")
    payload.update(kwargs)
    return ResearchRequest(**payload)


class TestSuccessMapping:
    def test_research_returns_answer_evidence_and_trace(self):
        tool = CompassRAGAgentTool(_agent())
        result = tool.research(_request())

        assert result.answer == "Paris"
        assert result.evidence, "真实 CompassRAG 应回传证据"
        top = result.evidence[0]
        assert top.chunk_id == "c1"
        assert top.source == "Alpha"          # store 里的 title
        assert top.retriever == "bm25"        # 检索器来源
        assert top.rank == 1                  # 排名
        assert top.quote == "Alpha text."
        assert result.trace_id and result.trace_id != "s1"
        assert result.retrieval_rounds == 1
        assert result.stop_reason == "answered"
        assert 0.0 < result.confidence <= 1.0

    def test_trace_can_be_read_back_without_recomputing(self):
        agent = _agent()
        tool = CompassRAGAgentTool(agent)
        result = tool.research(_request())
        calls_after_research = len(agent.llm.calls)

        trace = tool.get_trace(result.trace_id)
        assert trace is not None
        assert trace["question"] == "Where is Alpha?"
        assert trace["answer"] == "Paris"
        assert len(agent.llm.calls) == calls_after_research, "回读 trace 不应再调模型"

    def test_same_idempotency_key_does_not_rerun_agent(self):
        agent = _agent()
        tool = CompassRAGAgentTool(agent)
        first = tool.research(_request())
        second = tool.research(_request())   # 同 session/subtask → 同 idempotency_key
        assert first.trace_id == second.trace_id
        assert len(agent.llm.calls) == 1

    def test_evidence_serializes_retriever_and_rank(self):
        tool = CompassRAGAgentTool(_agent())
        evidence = tool.research(_request()).evidence[0].to_dict()
        assert evidence["retriever"] == "bm25" and evidence["rank"] == 1


class TestFailureMapping:
    def test_empty_evidence_is_no_evidence_error(self):
        tool = CompassRAGAgentTool(_agent(ranking=()))
        with pytest.raises(ResearchToolError) as excinfo:
            tool.research(_request())
        assert excinfo.value.kind == "no_evidence"
        assert excinfo.value.trace_id  # 失败也带 trace，便于回读现场

    def test_internal_exception_is_upstream_error(self):
        class BoomTool(CompassRAGAgentTool):
            def __init__(self):  # 跳过真实装配，直接塞一个会炸的 inner
                self.calls = 0

                class Inner:
                    def research(self, request):
                        raise RuntimeError("index corrupted")

                self._inner = Inner()

        with pytest.raises(ResearchToolError) as excinfo:
            BoomTool().research(_request())
        assert excinfo.value.kind == "upstream_error"
        assert "index corrupted" in excinfo.value.message

    def test_failure_table_covers_every_compass_category(self):
        assert set(_COMPASS_FAILURE_TO_ATLAS) == {
            "timeout", "budget_exhausted", "no_evidence",
            "upstream_error", "invalid_request", "cancelled",
        }
        assert set(_COMPASS_FAILURE_TO_ATLAS.values()) <= set(ERROR_KINDS)


class RecordingGraph:
    def __init__(self):
        self.notes = []

    def state_view(self, role):
        return f"（{role} 的状态视图）"

    def role_note(self, role, kind, content, reason="", evidence=""):
        self.notes.append({"role": role, "kind": kind, "content": content,
                           "reason": reason, "evidence": evidence})
        return {"node_id": f"{role}-{kind}", "kind": kind, "actor": role}

    def research_notes(self):
        return [n for n in self.notes if n["role"] == "research"]


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt):
        return self.replies.pop(0)


PLAN = json.dumps({"route": "先查 Alpha 的所在地", "risks": "证据不足", "reason": "题设依赖"})
AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})
PROBLEM = "题目：Alpha 在哪里？"


class TestBridgeInsideOrchestrator:
    def test_atlas_orchestration_writes_fact_with_real_compassrag_evidence(self):
        graph = RecordingGraph()
        tool = CompassRAGAgentTool(_agent())
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿解答", AUDIT_OK]),
            graph,
            machine=TaskStateMachine(),
            research_tool=tool,
        )

        answer, trace = orchestrator.solve(PROBLEM, session_id="s1", subtask_id="S2")

        assert answer == "草稿解答"
        notes = graph.research_notes()
        assert len(notes) == 1
        evidence = json.loads(notes[0]["evidence"])
        assert evidence[0]["source"] == "Alpha"
        assert evidence[0]["retriever"] == "bm25"
        assert trace.research_trace_id  # Atlas trace 里能下钻到 CompassRAG trace
        assert trace.research_rounds == 1
