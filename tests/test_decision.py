import json

from compassrag.decision.planner import (
    ROUTE_DIRECT,
    ROUTE_MULTI,
    ROUTE_SINGLE,
    make_plan,
    parse_plan_json,
)
from compassrag.decision.sufficiency import check_sufficiency, parse_sufficiency_json
from compassrag.llm.client import LLMResponse


class ScriptLLM:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens, "messages": messages})
        text = self.texts.pop(0) if self.texts else ""
        return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                           total_tokens=2, latency_ms=1.0,
                           finish_reason="stop" if text else "length")


def test_parse_plan_json_variants():
    assert parse_plan_json('{"route": "single", "subquestions": []}')["route"] == ROUTE_SINGLE
    fenced = "```json\n" + json.dumps({"route": "multi", "subquestions": ["a?", "b?"]}) + "\n```"
    assert parse_plan_json(fenced)["subquestions"] == ["a?", "b?"]
    assert parse_plan_json('{"route": "teleport"}') is None      # 未知路由
    assert parse_plan_json("no json") is None
    assert parse_plan_json("") is None


def test_make_plan_returns_route_and_caps_subquestions():
    subs = [f"sub {i}?" for i in range(7)]
    llm = ScriptLLM([json.dumps({"route": ROUTE_MULTI, "subquestions": subs})])
    plan = make_plan(llm, "q")
    assert plan.route == ROUTE_MULTI and len(plan.subquestions) == 4
    assert [c["tag"] for c in llm.calls] == ["plan"]
    assert len(plan.calls) == 1


def test_make_plan_falls_back_to_single_after_ladder():
    llm = ScriptLLM(["", "", ""])
    plan = make_plan(llm, "q")
    assert plan.route == ROUTE_SINGLE and plan.subquestions == []
    assert [c["tag"] for c in llm.calls] == ["plan", "plan_retry", "plan_retry"]
    assert len(plan.calls) == 3  # 成本聚合不丢


def test_parse_sufficiency_json_variants():
    assert parse_sufficiency_json('{"sufficient": true, "next_query": ""}') == {
        "sufficient": True, "next_query": ""}
    assert parse_sufficiency_json('{"sufficient": false, "next_query": "who wrote it?"}')["next_query"] == "who wrote it?"
    assert parse_sufficiency_json('{"sufficient": "yes"}') is None
    assert parse_sufficiency_json("") is None


def test_check_sufficiency_and_safe_fallback():
    llm = ScriptLLM([json.dumps({"sufficient": False, "next_query": "who wrote it?"})])
    suf = check_sufficiency(llm, "q", "evidence")
    assert suf.ok is False and suf.next_query == "who wrote it?"
    # 解析全败 → 降级为"充分"（停止循环，不无限烧预算）
    llm2 = ScriptLLM(["", "", ""])
    suf2 = check_sufficiency(llm2, "q", "evidence")
    assert suf2.ok is True and suf2.next_query == "" and len(suf2.calls) == 3
