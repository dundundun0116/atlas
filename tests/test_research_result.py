"""ResearchResult 对外契约（阶段 A）与本地 adapter（阶段 E 的前身）。

验收点：证据带原文片段与 retriever/rank；失败有五类可分支；幂等键返回同一 trace；
预算/轮数由请求决定，而不是由 agent 的默认配置决定。
"""

import json

import pytest
from pydantic import ValidationError

from compassrag.agent import (
    Agent,
    AgentConfig,
    ResearchRequest,
    StopReason,
    failure_category,
    research,
    to_research_result,
)
from compassrag.llm.client import LLMResponse


class FakeTool:
    def __init__(self, ranking=("c1", "c2")):
        self.ranking = list(ranking)

    def search(self, query, k=10):
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class BrokenTool:
    def search(self, query, k=10):
        raise RuntimeError("index exploded")


class ScriptLLM:
    def __init__(self, *texts, default="Paris"):
        self.queue = list(texts)
        self.default = default
        self.calls = 0

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls += 1
        text = self.queue.pop(0) if self.queue else self.default
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0, finish_reason="stop")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text. " * 60},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
}


def _agent(llm=None, tools=None, **kw):
    return Agent(llm or ScriptLLM(), tools or {"bm25": FakeTool()}, STORE,
                 AgentConfig(top_k=2, **kw))


# --------------------------------------------------------------------------- #
# 契约形状
# --------------------------------------------------------------------------- #


def test_research_request_rejects_empty_query():
    with pytest.raises(ValidationError):
        ResearchRequest(query="   ")


def test_result_carries_structured_evidence_with_quote_and_provenance():
    agent = _agent()
    out = to_research_result(agent.answer("q"), store=STORE)
    assert out.trace_id and out.stop_reason == "answered" and out.failure == ""
    assert [e.chunk_id for e in out.evidence] == ["c1", "c2"]
    first = out.evidence[0]
    assert first.source == "Alpha" and first.retriever == "bm25" and first.rank == 1
    assert first.quote.endswith("…")           # 长文本被截断
    assert out.retrieval_rounds == 1


def test_quote_length_is_capped():
    out = to_research_result(_agent().answer("q"), store=STORE, quote_chars=10)
    assert all(len(e.quote) <= 11 for e in out.evidence)


def test_confidence_is_bounded_and_zero_without_evidence():
    with_ev = to_research_result(_agent().answer("q"), store=STORE)
    assert 0.0 < with_ev.confidence <= 1.0
    empty = to_research_result(_agent(tools={"bm25": FakeTool([])}).answer("q"), store=STORE)
    assert empty.confidence == 0.0 and empty.failure == "no_evidence"
    assert empty.evidence == []


# --------------------------------------------------------------------------- #
# 失败分类：Atlas 只认这五类
# --------------------------------------------------------------------------- #


def test_failure_mapping_covers_all_stop_reasons():
    assert failure_category("answered") == ""
    assert failure_category("early_stop") == ""
    assert failure_category("empty_evidence") == "no_evidence"
    assert failure_category("insufficient_evidence") == "no_evidence"
    assert failure_category("tool_failure") == "upstream_error"
    assert failure_category("budget_exhausted:tokens") == "budget_exhausted"
    assert failure_category("budget_exhausted:rounds") == "budget_exhausted"
    assert failure_category("no_such_reason") == "upstream_error"


def test_broken_tool_becomes_upstream_error_not_exception():
    out = to_research_result(_agent(tools={"bm25": BrokenTool()}).answer("q"), store=STORE)
    assert out.failure == "upstream_error" and out.answer == "Paris"


def test_budget_exhausted_is_reported():
    llm = ScriptLLM(json.dumps({"route": "multi", "subquestions": ["s1?", "s2?"]}),
                    json.dumps({"sufficient": False, "next_query": "s3?"}), "Paris")
    agent = _agent(llm=llm, use_decision=True, max_rounds=5, max_total_tokens=20)
    out = to_research_result(agent.answer("q"), store=STORE)
    assert out.stop_reason == StopReason.BUDGET_TOKENS.value
    assert out.failure == "budget_exhausted"


# --------------------------------------------------------------------------- #
# 本地 adapter：请求里的预算/轮数生效，幂等键返回同一 trace
# --------------------------------------------------------------------------- #


def test_research_applies_request_budget_and_rounds():
    llm = ScriptLLM('{"route": "multi", "subquestions": ["s1?", "s2?", "s3?"]}',
                    json.dumps({"sufficient": False, "next_query": "s2?"}),
                    json.dumps({"sufficient": False, "next_query": "s3?"}), "Paris")
    agent = _agent(llm=llm, use_decision=True, max_rounds=99)
    out = research(agent, ResearchRequest(query="q", max_rounds=1))
    assert out.retrieval_rounds == 1          # 请求的 max_rounds 覆盖 agent 默认值


def test_idempotency_key_returns_same_trace():
    agent = _agent()
    cache: dict = {}
    req = ResearchRequest(query="q", idempotency_key="S3-attempt-1")
    first = research(agent, req, cache=cache)
    second = research(agent, req, cache=cache)
    assert first.trace_id == second.trace_id
    assert agent.llm.calls == 1               # 第二次没有再跑模型


def test_different_keys_do_not_collide():
    agent = _agent()
    cache: dict = {}
    a = research(agent, ResearchRequest(query="q", idempotency_key="a"), cache=cache)
    b = research(agent, ResearchRequest(query="q", idempotency_key="b"), cache=cache)
    assert a.trace_id != b.trace_id


# --------------------------------------------------------------------------- #
# "unknown" 不是成功：对外必须当失败信号，置信度归零
# --------------------------------------------------------------------------- #


def test_unknown_answer_is_reported_as_no_evidence():
    agent = _agent(llm=ScriptLLM("unknown"))
    out = to_research_result(agent.answer("q"), store=STORE)
    assert out.answer == "unknown"
    assert out.failure == "no_evidence"
    assert out.confidence == 0.0
    assert out.evidence          # 证据是有的，只是没答出来


def test_answerless_detection_is_whitespace_and_case_insensitive():
    from compassrag.agent.research import is_answerless

    assert is_answerless("  Unknown. ") and is_answerless("")
    assert not is_answerless("Unknown quantity")
