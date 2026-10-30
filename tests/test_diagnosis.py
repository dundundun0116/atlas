"""证据闭环（M2）：claim 核验 / 失败五分类 / 有限修复 / 拒答。

验收对应计划 M2：断言级支撑、五类失败、每类一个有限修复动作、修复有硬上限、
空证据不得强行生成事实。
"""

import json

from compassrag.agent import (
    Agent,
    AgentConfig,
    FailureClass,
    RepairAction,
    StopReason,
)
from compassrag.diagnose.repair import (
    choose_repair_query,
    classify_failures,
    plan_repair,
)
from compassrag.diagnose.verifier import parse_claims_json, resolve_claims, verify_answer
from compassrag.eval.traces import derive_row
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


class ScriptLLM:
    """按序吐文本，用完回落 default；记录 tag 便于断言调用链。"""

    def __init__(self, *texts, default="Paris"):
        self.queue = list(texts)
        self.default = default
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens})
        text = self.queue.pop(0) if self.queue else self.default
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0,
                           finish_reason="stop" if text.strip() else "length")

    @property
    def tags(self):
        return [c["tag"] for c in self.calls]


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
}


def _claims(*items, conflict=False):
    return json.dumps({"claims": [{"text": t, "evidence": e, "supported": s}
                                  for t, e, s in items], "source_conflict": conflict})


# --------------------------------------------------------------------------- #
# 解析与确定性引用校验
# --------------------------------------------------------------------------- #


def test_parse_claims_json_accepts_fenced_and_rejects_garbage():
    assert parse_claims_json('{"claims": []}') == {"claims": [], "source_conflict": False,
                                                   "conflict_note": ""}
    fenced = "```json\n" + _claims(("a", [1], True)) + "\n```"
    assert parse_claims_json(fenced)["claims"][0]["text"] == "a"
    assert parse_claims_json("no json") is None
    assert parse_claims_json('{"claims": "nope"}') is None
    assert parse_claims_json("") is None


def test_resolve_claims_validates_citations_deterministically():
    raw = {"claims": [
        {"text": "好的引用", "evidence": [1], "supported": True},
        {"text": "越界引用", "evidence": [9], "supported": True},
        {"text": "没给引用", "evidence": [], "supported": True},
        {"text": "模型说没支撑", "evidence": [1], "supported": False},
    ], "source_conflict": False, "conflict_note": ""}
    claims = resolve_claims(raw, ["c1", "c2"])
    assert claims[0].supported and claims[0].evidence_ids == ["c1"]
    assert not claims[1].supported and "不存在" in claims[1].reason
    assert not claims[2].supported and "未给出引用" in claims[2].reason
    assert not claims[3].supported


def test_verify_answer_resolves_claims_and_counts_calls():
    llm = ScriptLLM(_claims(("Paris", [1], True)))
    result, calls = verify_answer(llm, "q", "Paris", [("c1", "Alpha text.")])
    assert result.parsed and result.all_supported and len(calls) == 1
    assert result.claims[0].evidence_ids == ["c1"]


def test_verify_answer_without_evidence_never_calls_model():
    llm = ScriptLLM()
    result, calls = verify_answer(llm, "q", "", [])
    assert calls == [] and result.parsed and not result.claims


def test_verify_answer_parse_failure_is_not_treated_as_evidence_problem():
    llm = ScriptLLM("garbage", "garbage", "garbage")
    result, calls = verify_answer(llm, "q", "Paris", [("c1", "Alpha text.")])
    assert not result.parsed and not result.needs_repair  # 不触发修复
    assert len(calls) == 3                                 # 预算阶梯


# --------------------------------------------------------------------------- #
# 失败五分类与修复计划
# --------------------------------------------------------------------------- #


def test_classify_tool_failure_needs_all_tools_broken():
    assert classify_failures(tool_outcomes=["error", "ok"]) == []
    assert classify_failures(tool_outcomes=["error", "timeout"]) == [FailureClass.TOOL_FAILURE]


def test_classify_query_miss_needs_all_queries_empty():
    assert classify_failures(query_new_evidence=[0, 3]) == []
    assert classify_failures(query_new_evidence=[0, 0]) == [FailureClass.QUERY_MISS]


def test_classify_evidence_gap_requires_stuck_or_empty_answer():
    assert classify_failures(sufficiency_stuck=True) == [FailureClass.EVIDENCE_GAP]
    assert classify_failures(answer_empty=True, query_new_evidence=[2]) == [FailureClass.EVIDENCE_GAP]
    # 一条证据都没检到 → 是检索问题（query miss），不是"证据不足"
    assert classify_failures(answer_empty=True, query_new_evidence=[0]) == [FailureClass.QUERY_MISS]


def test_classify_answer_unsupported_and_conflict_come_from_verifier():
    from compassrag.agent import Claim, VerificationResult
    bad = VerificationResult(claims=[Claim(text="x", supported=False)], parsed=True)
    assert FailureClass.ANSWER_UNSUPPORTED in classify_failures(verification=bad)
    conflict = VerificationResult(claims=[Claim(text="x", supported=True)],
                                  parsed=True, source_conflict=True)
    assert classify_failures(verification=conflict) == [FailureClass.SOURCE_CONFLICT]


def test_plan_repair_is_bounded_and_maps_each_class():
    assert plan_repair([]) is None
    assert plan_repair([FailureClass.ANSWER_UNSUPPORTED], repairs_used=0,
                       max_repairs=1) is RepairAction.RETRIEVE_MORE
    assert plan_repair([FailureClass.QUERY_MISS], repairs_used=0,
                       max_repairs=1) is RepairAction.REWRITE_QUERY
    assert plan_repair([FailureClass.SOURCE_CONFLICT], repairs_used=0,
                       max_repairs=1) is RepairAction.SWITCH_VIEW
    # 修够了 / 上限为 0 → 拒答
    assert plan_repair([FailureClass.QUERY_MISS], repairs_used=1,
                       max_repairs=1) is RepairAction.REFUSE
    assert plan_repair([FailureClass.QUERY_MISS], repairs_used=0,
                       max_repairs=0) is RepairAction.REFUSE


def test_choose_repair_query_is_deterministic_and_short():
    long_claim = " ".join(f"w{i}" for i in range(30))
    assert choose_repair_query(RepairAction.RETRIEVE_MORE, claim_text=long_claim,
                               max_words=5) == "w0 w1 w2 w3 w4"
    assert choose_repair_query(RepairAction.RETRIEVE_MORE, next_query="who wrote it?") == "who wrote it"
    assert choose_repair_query(RepairAction.RETRIEVE_MORE, question="q?") == "q"
    assert choose_repair_query(RepairAction.REFUSE, claim_text="x") == ""


# --------------------------------------------------------------------------- #
# Agent 集成：核验 → 修复 → 拒答
# --------------------------------------------------------------------------- #


def _agent(llm, tool=None, **kw):
    return Agent(llm, {"bm25": tool or FakeTool(["c1", "c2"])}, STORE,
                 AgentConfig(top_k=2, **kw))


def test_unsupported_answer_triggers_one_repair_then_answers():
    llm = ScriptLLM(
        "Paris",
        _claims(("Paris is the capital of France", [1], False)),
        "Lyon",
        _claims(("Lyon", [1], True)),
    )
    result = _agent(llm, use_verifier=True, max_repairs=1).answer("Who is X?")
    assert result.answer == "Lyon"
    assert result.trace.repairs == [RepairAction.RETRIEVE_MORE]
    assert result.trace.n_answer_calls == 2
    assert "Paris is the capital of France" in result.queries   # 用未支撑断言做补检索
    assert result.stop_reason == StopReason.ANSWERED.value
    assert llm.tags == ["answer", "verify", "answer", "verify"]
    row = derive_row(result.trace, gold="Lyon", question_id="q1")
    assert row["repair_success"] is True and row["refused"] is False


def test_repair_budget_zero_refuses_instead_of_answering():
    llm = ScriptLLM("Paris", _claims(("Paris", [1], False)))
    result = _agent(llm, use_verifier=True, max_repairs=0).answer("q")
    assert result.answer == "unknown"
    assert result.stop_reason == StopReason.INSUFFICIENT_EVIDENCE.value
    assert result.trace.repairs == [RepairAction.REFUSE]
    assert llm.tags == ["answer", "verify"]        # 没有再答一次


def test_supported_answer_is_not_repaired():
    llm = ScriptLLM("Paris", _claims(("Paris", [1], True)))
    result = _agent(llm, use_verifier=True).answer("q")
    assert result.answer == "Paris"
    assert result.trace.repairs == [] and result.trace.failures == []
    assert result.trace.verification.all_supported


def test_second_verification_failure_refuses():
    llm = ScriptLLM(
        "Paris", _claims(("Paris", [1], False)),
        "Lyon", _claims(("Lyon", [1], False)),
    )
    result = _agent(llm, use_verifier=True, max_repairs=1).answer("q")
    assert result.answer == "unknown"
    assert result.stop_reason == StopReason.INSUFFICIENT_EVIDENCE.value
    assert result.trace.repairs == [RepairAction.RETRIEVE_MORE, RepairAction.REFUSE]


def test_verifier_parse_failure_keeps_answer_and_counts_fallback():
    llm = ScriptLLM("Paris", default="not json")
    result = _agent(llm, use_verifier=True).answer("q")
    assert result.answer == "Paris"
    assert result.trace.parse_fallbacks == 1
    assert result.trace.repairs == []


def test_refuse_on_empty_evidence_skips_model_entirely():
    llm = ScriptLLM()
    result = _agent(llm, tool=FakeTool([]), refuse_on_empty_evidence=True).answer("q")
    assert result.answer == "unknown"
    assert result.stop_reason == StopReason.EMPTY_EVIDENCE.value
    assert llm.calls == []            # 没证据就不编：一次调用都不发
    assert result.trace.n_answer_calls == 0


def test_verification_tokens_count_toward_budget():
    llm = ScriptLLM("Paris", _claims(("Paris", [1], True)))
    result = _agent(llm, use_verifier=True).answer("q")
    assert result.n_llm_calls == 2                  # answer + verify
    assert result.prompt_tokens == 20               # 10 × 2


# --------------------------------------------------------------------------- #
# switch_view 修复：真的换一路检索，而不是把三路重打一遍
# --------------------------------------------------------------------------- #


class CountingTool(FakeTool):
    def __init__(self, name, ranking=("c1", "c2")):
        super().__init__(ranking)
        self.name = name


def test_switch_view_repair_only_queries_one_retriever():
    from compassrag.agent import RepairAction

    llm = ScriptLLM(
        "Paris",
        json.dumps({"claims": [{"text": "Paris", "evidence": [1], "supported": True}],
                    "source_conflict": True}),   # 冲突 → SWITCH_VIEW
        "Lyon",
        _claims(("Lyon", [1], True)),
    )
    bm25, dense = CountingTool("bm25"), CountingTool("dense")
    agent = Agent(llm, {"bm25": bm25, "dense": dense}, STORE,
                  AgentConfig(top_k=2, use_verifier=True, max_repairs=1))
    result = agent.answer("q")
    assert result.trace.repairs == [RepairAction.SWITCH_VIEW]
    # 修复那一轮只走一路（dense），而不是两路都打
    assert len(bm25.queries) == 1 and len(dense.queries) == 2


def test_repair_views_returns_none_without_alternative():
    from compassrag.agent import RepairAction

    agent = Agent(ScriptLLM(), {"bm25": FakeTool()}, STORE, AgentConfig())
    assert agent.repair_views(RepairAction.SWITCH_VIEW) is None      # 只有一路，无从切换
    assert agent.repair_views(RepairAction.RETRIEVE_MORE) is None    # 其它动作走全量


def test_source_conflict_triggers_switch_view_then_succeeds():
    llm = ScriptLLM(
        "Paris",
        json.dumps({"claims": [{"text": "Paris", "evidence": [1], "supported": True}],
                    "source_conflict": True}),
        "Lyon",
        json.dumps({"claims": [{"text": "Lyon", "evidence": [1], "supported": True}],
                    "source_conflict": False}),
    )
    agent = Agent(llm, {"bm25": FakeTool(), "dense": FakeTool()}, STORE,
                  AgentConfig(top_k=2, use_verifier=True, max_repairs=1))
    from compassrag.agent import RepairAction
    result = agent.answer("q")
    assert result.trace.repairs == [RepairAction.SWITCH_VIEW]
    assert result.answer == "Lyon" and result.stop_reason == "answered"
