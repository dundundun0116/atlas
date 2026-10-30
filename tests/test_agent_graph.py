"""阶段 C 验收：LangGraph runner 与 `Agent.answer` 必须给出**同一份结果**。

对照口径：answer / evidence / rounds / queries / n_llm_calls / n_calls / stop_reason / tokens。
只要有一项不同，就说明图与顺序实现的语义已经分叉——那正是 `lessons/E2` 要拦的事。
"""

import json

from compassrag.agent import Agent, AgentConfig
from compassrag.agent.graph import run_graph
from compassrag.llm.client import LLMResponse


class FakeTool:
    def __init__(self, ranking=("c1", "c2", "c3")):
        self.ranking = list(ranking)
        self.queries = []

    def search(self, query, k=10):
        self.queries.append(query)
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class ScriptLLM:
    def __init__(self, *texts, default="Paris"):
        self.queue = list(texts)
        self.default = default
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append(tag)
        text = self.queue.pop(0) if self.queue else self.default
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0,
                           finish_reason="stop" if text.strip() else "length")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
    "c3": {"chunk_id": "c3", "title": "Gamma", "text": "Gamma text."},
}


def _plan(route="single", subs=()):
    return json.dumps({"route": route, "subquestions": list(subs)})


def _suf(ok, next_query=""):
    return json.dumps({"sufficient": ok, "next_query": next_query})


def _claims(*items, conflict=False):
    return json.dumps({"claims": [{"text": t, "evidence": e, "supported": s}
                                  for t, e, s in items], "source_conflict": conflict})


def _pair(script, *, cfg, tools=None):
    """两套完全相同的依赖：一套跑顺序实现，一套跑图。"""
    def make():
        return ScriptLLM(*script), (tools or FakeTool())
    llm_a, tool_a = make()
    llm_b, tool_b = make()
    a = Agent(llm_a, {"bm25": tool_a}, STORE, cfg)
    b = Agent(llm_b, {"bm25": tool_b}, STORE, cfg)
    return a, b, tool_a, tool_b


def _compare(a_result, g_result, *, expected_queries=()):
    assert g_result.answer == a_result.answer
    assert g_result.evidence == a_result.evidence
    assert g_result.rounds == a_result.rounds
    assert g_result.queries == a_result.queries
    assert g_result.n_llm_calls == a_result.n_llm_calls
    assert g_result.n_calls == a_result.n_calls
    assert g_result.stop_reason == a_result.stop_reason
    assert g_result.prompt_tokens == a_result.prompt_tokens
    if expected_queries:
        assert list(g_result.queries) == list(expected_queries)


def test_graph_matches_scripted_naive():
    a, b, ta, tb = _pair(["Paris"], cfg=AgentConfig(top_k=2))
    _compare(a.answer("Who is Alpha?"), run_graph(b, "Who is Alpha?"))
    assert ta.queries == tb.queries == ["Who is Alpha?"]


def test_graph_matches_scripted_with_rewrite():
    script = ["- Who created it?\n- When born?", "A passage about it.", "Paris"]
    a, b, ta, tb = _pair(script, cfg=AgentConfig(top_k=2, use_rewrite=True))
    _compare(a.answer("When is the birthday of the creator of it?"),
             run_graph(b, "When is the birthday of the creator of it?"))
    assert ta.queries == tb.queries and len(ta.queries) == 4


def test_graph_matches_decision_early_stop():
    script = [_plan("multi", ["who made it?", "when was it made?"]),
              _suf(True), "1819"]
    a, b, ta, tb = _pair(script, cfg=AgentConfig(top_k=2, use_decision=True, max_rounds=3))
    _compare(a.answer("when was it made?"), run_graph(b, "when was it made?"))
    assert ta.queries[0] == tb.queries[0] == "when was it made?"


def test_graph_matches_decision_round_budget():
    script = [_plan("multi", ["s1?", "s2?", "s3?"]), _suf(False, "s2?"),
              _suf(False, "s3?"), "Paris"]
    a, b, ta, tb = _pair(script, cfg=AgentConfig(top_k=2, use_decision=True, max_rounds=2))
    ra, rb = a.answer("q"), run_graph(b, "q")
    _compare(ra, rb)
    assert rb.stop_reason == "budget_exhausted:rounds" and rb.rounds == 2


def test_graph_matches_direct_route():
    a, b, ta, tb = _pair([_plan("direct", []), "Paris"],
                         cfg=AgentConfig(use_decision=True))
    ra, rb = a.answer("What is the capital of France?"), run_graph(b, "What is the capital of France?")
    _compare(ra, rb)
    assert rb.rounds == 0 and tb.queries == []


def test_graph_matches_empty_evidence_refusal():
    a, b, ta, tb = _pair(["Paris"], cfg=AgentConfig(refuse_on_empty_evidence=True),
                         tools=FakeTool([]))
    ra, rb = a.answer("q"), run_graph(b, "q")
    _compare(ra, rb)
    assert rb.answer == "unknown" and rb.stop_reason == "empty_evidence"


def test_graph_budget_refuses_before_answer_call():
    script = [_plan("multi", ["s1?"]), _suf(True), "should-not-run"]
    a, b, _, _ = _pair(script, cfg=AgentConfig(top_k=2, use_decision=True,
                                               max_total_tokens=20))
    ra, rb = a.answer("q"), run_graph(b, "q")
    assert ra.answer == rb.answer == "unknown"
    assert ra.stop_reason == rb.stop_reason == "budget_exhausted:tokens"
    assert ra.n_calls == rb.n_calls == 0


def test_graph_cancellation_stops_before_verifier_call():
    llm = ScriptLLM("Paris", _claims(("Paris", [1], True)))
    agent = Agent(llm, {"bm25": FakeTool()}, STORE,
                 AgentConfig(top_k=2, use_verifier=True))
    result = agent.answer("q", cancel_check=lambda: len(llm.calls) >= 1)
    assert result.stop_reason == "cancelled"
    assert result.answer == ""
    assert llm.calls == ["answer"]


def test_graph_cancellation_stops_rewrite_ladder_before_hyde():
    llm = ScriptLLM("- sub-question?")
    tool = FakeTool()
    agent = Agent(llm, {"bm25": tool}, STORE,
                 AgentConfig(top_k=2, use_rewrite=True))
    result = agent.answer("q", cancel_check=lambda: len(llm.calls) >= 1)
    assert result.stop_reason == "cancelled"
    assert result.answer == ""
    assert llm.calls == ["decompose"]
    assert tool.queries == []


def test_graph_matches_verifier_repair_then_success():
    script = ["Paris", _claims(("Paris is the capital", [1], False)),
              "Lyon", _claims(("Lyon", [1], True))]
    a, b, ta, tb = _pair(script, cfg=AgentConfig(top_k=2, use_verifier=True, max_repairs=1))
    ra, rb = a.answer("q"), run_graph(b, "q")
    _compare(ra, rb)
    assert rb.answer == "Lyon" and [r.value for r in rb.trace.repairs] == ["retrieve_more"]


def test_graph_matches_verifier_refusal_when_repairs_exhausted():
    script = ["Paris", _claims(("Paris", [1], False))]
    a, b, ta, tb = _pair(script, cfg=AgentConfig(top_k=2, use_verifier=True, max_repairs=0))
    ra, rb = a.answer("q"), run_graph(b, "q")
    _compare(ra, rb)
    assert rb.answer == "unknown" and rb.stop_reason == "insufficient_evidence"


def test_graph_keeps_trace_off_framework_state():
    """框架状态里不存业务事实：证据/停止原因都在 AnswerTrace 上。"""
    a, b, ta, tb = _pair(["Paris"], cfg=AgentConfig(top_k=2))
    rb = run_graph(b, "q")
    assert rb.trace is not None
    assert rb.trace.evidence_ids == rb.evidence
    assert rb.trace.stop_reason.value == rb.stop_reason
    assert [s.kind for s in rb.trace.steps] == ["retrieve", "answer", "finish"]


# --------------------------------------------------------------------------- #
# 公开 API 层面的等价：`Agent(answer)` 在 use_graph 开/关下必须一致
# --------------------------------------------------------------------------- #


def test_agent_api_is_equivalent_with_and_without_graph():
    scenarios = {
        "naive": (["Paris"], AgentConfig(top_k=2)),
        "rewrite": (["- a?\n- b?", "passage", "Paris"],
                    AgentConfig(top_k=2, use_rewrite=True)),
        "decision": ([_plan("multi", ["s1?", "s2?"]), _suf(True), "Paris"],
                     AgentConfig(top_k=2, use_decision=True, max_rounds=3)),
        "verifier": (["Paris", _claims(("Paris", [1], True))],
                     AgentConfig(top_k=2, use_verifier=True)),
    }
    for name, (script, cfg) in scenarios.items():
        off = Agent(ScriptLLM(*script), {"bm25": FakeTool()}, STORE,
                    AgentConfig(**{**cfg.__dict__, "use_graph": False}))
        on = Agent(ScriptLLM(*script), {"bm25": FakeTool()}, STORE,
                   AgentConfig(**{**cfg.__dict__, "use_graph": True}))
        a, b = off.answer("q"), on.answer("q")
        assert (a.answer, a.evidence, a.rounds, a.queries, a.stop_reason) == \
               (b.answer, b.evidence, b.rounds, b.queries, b.stop_reason), name


def test_default_config_uses_graph():
    assert AgentConfig().use_graph is True


# --------------------------------------------------------------------------- #
# 答案词数上限：默认不改提示词（保锚点），开启后才追加约束
# --------------------------------------------------------------------------- #


def test_answer_prompt_is_byte_identical_by_default():
    from compassrag.agent.core import ANSWER_PROMPT

    agent = Agent(ScriptLLM("Paris"), {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2))
    st = agent._new_state("q")
    st.add_evidence(["c1", "c2"])
    expected = ANSWER_PROMPT.format(context=agent._format(st.evidence), question="q")
    assert agent._answer_prompt(st) == expected
    assert "at most" not in agent._answer_prompt(st)


def test_strict_answer_words_appends_constraint_only_when_enabled():
    agent = Agent(ScriptLLM("Paris"), {"bm25": FakeTool()}, STORE,
                  AgentConfig(top_k=2, strict_answer_words=5))
    prompt = agent._answer_prompt(agent._new_state("q"))
    assert "at most 5 words" in prompt and "no parentheticals" in prompt


def test_direct_route_uses_common_knowledge_prompt():
    """direct 路由不能用"只准用证据"的提示词，否则常识题会被逼成 unknown。"""
    from compassrag.agent.core import DIRECT_ANSWER_PROMPT
    from compassrag.agent.schemas import Route

    llm = ScriptLLM(_plan("direct", []), "Paris")
    agent = Agent(llm, {"bm25": FakeTool()}, STORE, AgentConfig(use_decision=True))
    result = agent.answer("What is the capital of France?")
    assert result.route == "direct" and result.answer == "Paris"

    st = agent._new_state("q")
    st.route = Route.DIRECT
    prompt = agent._answer_prompt(st)
    assert prompt == DIRECT_ANSWER_PROMPT.format(question="q")
    assert "based only on the evidence" not in prompt
