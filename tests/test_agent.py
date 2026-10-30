from compassrag.agent import RETRY_NUDGE, Agent, AgentConfig
from compassrag.llm.client import LLMResponse


class FakeTool:
    def __init__(self, ranking):
        self.ranking = ranking
        self.queries = []

    def search(self, query, k=10):
        self.queries.append(query)
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class FakeLLM:
    def __init__(self, text="Paris"):
        self.text = text
        self.queue: list[str] = []
        self.calls = []

    def script(self, texts):
        """按序返回给定文本，用完后回落 self.text。"""
        self.queue = list(texts)
        return self

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"messages": messages, "tag": tag, "max_tokens": max_tokens})
        text = self.queue.pop(0) if self.queue else self.text
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0,
                           finish_reason="stop" if text.strip() else "length")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
    "c3": {"chunk_id": "c3", "title": "Gamma", "text": "Gamma text."},
}


def test_scripted_single_round_single_tool():
    llm, tool = FakeLLM(), FakeTool(["c1", "c2", "c3"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(top_k=2))
    result = agent.answer("Where is Alpha?")
    assert result.answer == "Paris"
    assert result.rounds == 1
    assert result.evidence == ["c1", "c2"]  # top_k 截断
    assert len(llm.calls) == 1 and llm.calls[0]["tag"] == "answer"
    assert tool.queries == ["Where is Alpha?"]


def test_multi_tool_rrf_fusion_orders_by_consensus():
    a, b = FakeTool(["c1", "c2"]), FakeTool(["c2", "c3"])
    agent = Agent(FakeLLM(), {"bm25": a, "dense": b}, STORE, AgentConfig(top_k=3))
    result = agent.answer("q")
    assert result.evidence[0] == "c2"  # 两路都命中，RRF 最高


def test_prompt_carries_evidence_and_question():
    llm = FakeLLM()
    agent = Agent(llm, {"bm25": FakeTool(["c1"])}, STORE, AgentConfig(top_k=1))
    agent.answer("Who is Beta?")
    user_content = llm.calls[0]["messages"][-1]["content"]
    assert "Alpha" in user_content and "Alpha text." in user_content
    assert "Who is Beta?" in user_content


def test_no_evidence_still_answers_with_placeholder():
    agent = Agent(FakeLLM(), {"bm25": FakeTool([])}, STORE, AgentConfig(top_k=3))
    result = agent.answer("q")
    assert result.evidence == [] and result.answer == "Paris"


def test_empty_answer_triggers_nudge_retry_with_bigger_budget():
    llm = FakeLLM().script(["", "Paris"])  # 首轮撞预算为空 → 重试出答案
    agent = Agent(llm, {"bm25": FakeTool(["c1"])}, STORE, AgentConfig(top_k=1))
    result = agent.answer("q")
    assert result.answer == "Paris" and result.n_calls == 2
    assert [c["tag"] for c in llm.calls] == ["answer", "answer_retry"]
    retry_msgs = llm.calls[1]["messages"]
    assert retry_msgs[-1]["content"] == RETRY_NUDGE
    assert llm.calls[1]["max_tokens"] > llm.calls[0]["max_tokens"]
    assert result.finish_reasons == ["length", "stop"]
    assert result.prompt_tokens == 20  # 两次调用聚合


def test_retry_cascade_escalates_budget_until_answer():
    llm = FakeLLM().script(["", "", "Paris"])  # 两级重试都在前两次落空
    agent = Agent(llm, {"bm25": FakeTool(["c1"])}, STORE,
                  AgentConfig(top_k=1, answer_retry_budgets=(2048, 4096)))
    result = agent.answer("q")
    assert result.answer == "Paris" and result.n_calls == 3
    budgets = [c["max_tokens"] for c in llm.calls]
    assert budgets == [1024, 2048, 4096]
    assert result.finish_reasons == ["length", "length", "stop"]


def test_empty_after_all_retries_kept_empty_with_flags():
    llm = FakeLLM().script(["", "", ""])
    agent = Agent(llm, {"bm25": FakeTool(["c1"])}, STORE, AgentConfig(top_k=1))
    result = agent.answer("q")
    assert result.answer == "" and result.n_calls == 3
    assert result.finish_reasons == ["length", "length", "length"]


def test_nonempty_answer_skips_retry():
    llm = FakeLLM()
    agent = Agent(llm, {"bm25": FakeTool(["c1"])}, STORE, AgentConfig(top_k=1))
    result = agent.answer("q")
    assert result.n_calls == 1 and result.finish_reasons == ["stop"]
    assert [c["tag"] for c in llm.calls] == ["answer"]
    assert result.queries == ["q"]  # ② 关闭时只有原问题


class VectorTool(FakeTool):
    """带向量的假检索工具（供近重合并路径）。"""

    def __init__(self, ranking, vectors):
        super().__init__(ranking)
        self.vectors = vectors

    def vector(self, cid):
        return self.vectors.get(cid)


def test_rewrite_expands_queries_and_searches_every_variant():
    llm = FakeLLM().script([
        "- Who created it?\n- When was that person born?",   # decompose
        "It was created by Someone, born in 1819.",          # hyde
        "Paris",                                            # answer
    ])
    tool = FakeTool(["c1", "c2"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(top_k=2, use_rewrite=True))
    result = agent.answer("When is the birthday of the creator of it?")
    assert result.answer == "Paris"
    assert result.queries[0] == "When is the birthday of the creator of it?"
    assert len(result.queries) == 4  # 原问题 + 2 子问题 + HyDE
    assert len(tool.queries) == 4    # 每个变体都检索了
    assert tool.queries[-1].startswith("It was created by")


def test_rewrite_near_dup_merge_drops_duplicate_evidence():
    import numpy as np
    llm = FakeLLM().script(["", ""])  # 分解/HyDE 都失败 → 只有原问题，走融合+合并
    vecs = {"c1": np.array([1.0, 0.0], dtype=np.float32),
            "c2": np.array([0.99, 0.05], dtype=np.float32),  # 与 c1 近重（cos>0.92）
            "c3": np.array([0.0, 1.0], dtype=np.float32)}
    tool = VectorTool(["c1", "c2", "c3"], vecs)
    agent = Agent(llm, {"bm25": tool, "dense": tool}, STORE,
                  AgentConfig(top_k=3, use_rewrite=True))
    result = agent.answer("q")
    assert result.evidence == ["c1", "c3"]  # c2 与 c1 近重被合并


# ---- ① 决策形态（S5）----

def _plan_multi(subs):
    import json as _json
    return _json.dumps({"route": "multi", "subquestions": subs})


def _suf(ok, next_query=""):
    import json as _json
    return _json.dumps({"sufficient": ok, "next_query": next_query})


def test_agentic_loop_plans_retrieves_early_stops_and_answers():
    llm = FakeLLM().script([
        _plan_multi(["Who created it?", "When was that person born?"]),  # plan
        _suf(False, "Who created it exactly?"),                          # 第 1 轮：不够，给下一查询
        _suf(True),                                                      # 第 2 轮：够了 → 早停
        "1819",                                                          # answer
    ])
    tool = FakeTool(["c1", "c2", "c3"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(top_k=2, use_decision=True, max_rounds=3))
    result = agent.answer("When was the creator born?")
    assert result.answer == "1819"
    assert result.route == "multi" and result.rounds == 2
    assert tool.queries == ["When was the creator born?", "Who created it exactly?"]
    assert result.queries == ["When was the creator born?", "Who created it exactly?"]
    assert [c["tag"] for c in llm.calls] == ["plan", "sufficiency", "sufficiency", "answer"]
    assert result.n_llm_calls == 4 and result.n_calls == 1
    assert result.prompt_tokens == 40  # 全部调用聚合（FakeLLM 每次 10 token × 4 次）


def test_agentic_loop_respects_max_rounds_budget():
    llm = FakeLLM().script([
        _plan_multi(["s1?", "s2?", "s3?"]),
        _suf(False, "s2?"),   # 一直不够
        _suf(False, "s3?"),
        "Paris",              # 预算耗尽后仍作答
    ])
    tool = FakeTool(["c1", "c2"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(top_k=2, use_decision=True, max_rounds=2))
    result = agent.answer("q")
    assert result.rounds == 2 and result.answer == "Paris"
    assert len(tool.queries) == 2
    assert [c["tag"] for c in llm.calls] == ["plan", "sufficiency", "sufficiency", "answer"]


def test_agentic_direct_route_skips_retrieval():
    llm = FakeLLM().script(['{"route": "direct", "subquestions": []}', "Paris"])
    tool = FakeTool(["c1"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(use_decision=True))
    result = agent.answer("What is the capital of France?")
    assert result.route == "direct" and result.rounds == 0 and result.evidence == []
    assert tool.queries == []
    assert result.answer == "Paris"
    assert [c["tag"] for c in llm.calls] == ["plan", "answer"]


def test_agentic_single_route_uses_original_question_once():
    llm = FakeLLM().script(['{"route": "single", "subquestions": []}', _suf(True), "Paris"])
    tool = FakeTool(["c1"])
    agent = Agent(llm, {"bm25": tool}, STORE, AgentConfig(use_decision=True, max_rounds=3))
    result = agent.answer("Who is X?")
    assert result.rounds == 1 and tool.queries == ["Who is X?"]
    assert [c["tag"] for c in llm.calls] == ["plan", "sufficiency", "answer"]
