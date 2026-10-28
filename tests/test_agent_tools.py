"""工具协议（M1）与 agent 韧性测试。

覆盖计划 M1 的验收：工具错误不外泄、超时可控、失败可解释、答案能列出工具与证据。
"""

import time

import pytest
from pydantic import ValidationError

from compassrag.agent import (
    Agent,
    AgentConfig,
    SearchRequest,
    StopReason,
    ToolSpec,
    build_registry,
)
from compassrag.llm.client import LLMResponse


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class FakeTool:
    def __init__(self, ranking=("c1", "c2")):
        self.ranking = list(ranking)
        self.queries = []

    def search(self, query, k=10):
        self.queries.append(query)
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class BrokenTool:
    def __init__(self, exc):
        self.exc = exc

    def search(self, query, k=10):
        raise self.exc


class SlowTool:
    def search(self, query, k=10):
        time.sleep(0.2)
        return [("c1", 1.0)]


class ShapeTool:
    def search(self, query, k=10):
        return [("c1",)]  # 形状非法


class FakeLLM:
    def __init__(self, text="Paris"):
        self.text = text
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens})
        return LLMResponse(text=self.text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0, finish_reason="stop")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
}


# --------------------------------------------------------------------------- #
# SearchRequest
# --------------------------------------------------------------------------- #


def test_search_request_rejects_empty_query():
    with pytest.raises(ValidationError):
        SearchRequest(query="   ")


def test_search_request_input_hash_is_stable_and_sensitive():
    a = SearchRequest(query="q", top_k=5)
    assert a.input_hash() == SearchRequest(query="q", top_k=5).input_hash()
    assert a.input_hash() != SearchRequest(query="q", top_k=6).input_hash()


# --------------------------------------------------------------------------- #
# ToolSpec：统一协议与错误分类
# --------------------------------------------------------------------------- #


def test_tool_spec_ok_keeps_retriever_and_rank():
    call = ToolSpec("bm25", FakeTool(["c2", "c1"])).run(SearchRequest(query="q", top_k=2))
    assert call.outcome == "ok" and call.ok
    assert [h.chunk_id for h in call.hits] == ["c2", "c1"]
    assert [h.rank for h in call.hits] == [1, 2]
    assert {h.retriever for h in call.hits} == {"bm25"}


def test_tool_spec_empty_is_not_error():
    call = ToolSpec("bm25", FakeTool([])).run(SearchRequest(query="q"))
    assert call.outcome == "empty" and call.hits == []


def test_tool_spec_error_does_not_escape():
    call = ToolSpec("kg", BrokenTool(RuntimeError("boom"))).run(SearchRequest(query="q"))
    assert call.outcome == "error" and "boom" in call.error


def test_tool_spec_index_missing():
    call = ToolSpec("kg", BrokenTool(FileNotFoundError("no graph"))).run(SearchRequest(query="q"))
    assert call.outcome == "index_missing"


def test_tool_spec_invalid_shape():
    call = ToolSpec("bm25", ShapeTool()).run(SearchRequest(query="q"))
    assert call.outcome == "invalid_params"


def test_tool_spec_timeout_is_enforced():
    spec = ToolSpec("dense", SlowTool(), timeout_s=0.05)
    call = spec.run(SearchRequest(query="q"))
    assert call.outcome == "timeout"


# --------------------------------------------------------------------------- #
# 注册表：一个工具坏掉不影响其他工具
# --------------------------------------------------------------------------- #


def test_registry_isolates_failures():
    reg = build_registry({"bm25": FakeTool(["c1"]), "kg": BrokenTool(RuntimeError("x"))})
    calls = reg.run_all(SearchRequest(query="q", top_k=1))
    assert [c.outcome for c in calls] == ["ok", "error"]
    assert calls[0].chunk_ids == ["c1"]


# --------------------------------------------------------------------------- #
# Agent 韧性 + 轨迹
# --------------------------------------------------------------------------- #


def test_agent_survives_broken_tool_and_records_trace():
    agent = Agent(FakeLLM(), {"bm25": FakeTool(["c1"]), "kg": BrokenTool(RuntimeError("x"))},
                  STORE, AgentConfig(top_k=1))
    result = agent.answer("Who is Alpha?")
    assert result.answer == "Paris"
    assert result.evidence == ["c1"]
    trace = result.trace
    assert trace is not None
    assert len(trace.tool_calls) == 2
    assert sum(1 for c in trace.tool_calls if not c.ok) == 1
    assert trace.stop_reason == StopReason.ANSWERED
    assert trace.queries[0].query == "Who is Alpha?"


def test_all_tools_broken_still_answers_and_says_tool_failure():
    agent = Agent(FakeLLM(), {"bm25": BrokenTool(RuntimeError("x"))}, STORE, AgentConfig())
    result = agent.answer("q")
    assert result.answer == "Paris"          # 旧行为：无证据也作答，绝不崩
    assert result.stop_reason == StopReason.TOOL_FAILURE.value


def test_empty_hits_give_empty_evidence_stop_reason():
    agent = Agent(FakeLLM(), {"bm25": FakeTool([])}, STORE, AgentConfig())
    result = agent.answer("q")
    assert result.stop_reason == StopReason.EMPTY_EVIDENCE.value


def test_trace_records_query_sources_and_answer_step():
    agent = Agent(FakeLLM(), {"bm25": FakeTool(["c1"])}, STORE, AgentConfig(top_k=1))
    result = agent.answer("q")
    trace = result.trace
    assert [q.source.value for q in trace.queries] == ["original"]
    assert [s.kind for s in trace.steps] == ["retrieve", "answer", "finish"]
    assert trace.evidence_titles == ["Alpha"]
    assert trace.n_answer_calls == 1 and trace.n_llm_calls == 1


def test_token_budget_stops_decision_loop():
    """预算触顶必须收敛并写清 budget_exhausted:tokens，而不是继续查。"""
    import json as _json

    class BudgetLLM(FakeLLM):
        def __init__(self):
            super().__init__("Paris")
            self.queue = [
                _json.dumps({"route": "multi", "subquestions": ["s1?", "s2?"]}),
                _json.dumps({"sufficient": False, "next_query": "s3?"}),
            ]

        def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
            super().chat(messages, tag=tag, max_tokens=max_tokens)
            text = self.queue.pop(0) if self.queue else self.text
            return LLMResponse(text=text, model="fake", prompt_tokens=100, completion_tokens=100,
                               total_tokens=200, latency_ms=1.0, finish_reason="stop")

    agent = Agent(BudgetLLM(), {"bm25": FakeTool(["c1", "c2"])}, STORE,
                  AgentConfig(use_decision=True, max_rounds=5, max_total_tokens=250))
    result = agent.answer("q")
    assert result.stop_reason == StopReason.BUDGET_TOKENS.value
    assert result.rounds == 1                  # 第二轮开始前就被预算拦下
    assert result.answer == "Paris"
