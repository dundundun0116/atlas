"""问题路由与检索计划（① 的入口决策）：LLM 判定 direct / single / multi。

- direct：无需检索（常识题）——不发起任何检索轮；
- single：单轮检索够用（指向单一事实/单篇文章）；
- multi：需要多轮；给出 2-4 个自足子问题，按顺序逐轮检索。

解析失败一律降级为 single（[question]），绝不让决策组件把整题拖死。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Callable

ROUTE_DIRECT = "direct"
ROUTE_SINGLE = "single"
ROUTE_MULTI = "multi"

PLAN_NUDGE = "Output the JSON now. Do not analyze further."
PLAN_BUDGETS = (1024, 2048, 4096)

PLAN_PROMPT = """Decide how to answer a multi-hop question with retrieval.

Routes:
- "direct": the answer is common knowledge; no retrieval needed.
- "single": one retrieval round suffices (the question targets a single fact or article).
- "multi": needs several retrieval rounds; provide 2-4 self-contained sub-questions, in order.
- Each sub-question: name entities explicitly and keep it under 15 words (retrieval queries are keyword-like).

Output JSON only, no other text:
{{"route": "single", "subquestions": []}}

Examples:
Q: What is the capital of France?
{{"route": "direct", "subquestions": []}}
Q: In which country is the city of Sikyona located?
{{"route": "single", "subquestions": []}}
Q: When is the birthday of the creator of Le ruisseau noir?
{{"route": "multi", "subquestions": ["Who created Le ruisseau noir?", "When was that person born?"]}}
Q: Who wrote the novel that the film Blade Runner was based on?
{{"route": "multi", "subquestions": ["Which novel was the film Blade Runner based on?", "Who wrote that novel?"]}}

Question: {question}"""


@dataclass
class Plan:
    route: str = ROUTE_SINGLE
    subquestions: list[str] = field(default_factory=list)
    calls: list = field(default_factory=list)   # 本次决策消耗的 LLM 调用（供成本聚合）
    parsed: bool = True                         # False = 解析全败后的安全降级
    fallback_reason: str = ""


def parse_plan_json(text: str) -> dict | None:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("route") not in (ROUTE_DIRECT, ROUTE_SINGLE, ROUTE_MULTI):
        return None
    subs = obj.get("subquestions", [])
    if not isinstance(subs, list):
        subs = []
    return {"route": obj["route"], "subquestions": [s for s in subs if isinstance(s, str) and s.strip()]}


def make_plan(llm, question: str, *, budgets: tuple = PLAN_BUDGETS, max_sub: int = 4,
              cancel_check: Callable[[], bool] | None = None) -> Plan:
    messages = [{"role": "system", "content": "You plan retrieval for multi-hop questions."},
                {"role": "user", "content": PLAN_PROMPT.format(question=question)}]
    calls: list = []
    for n, budget in enumerate(budgets):
        if cancel_check is not None and cancel_check():
            break
        msgs = messages if n == 0 else messages + [{"role": "user", "content": PLAN_NUDGE}]
        resp = llm.chat(msgs, tag="plan" if n == 0 else "plan_retry", max_tokens=int(budget))
        calls.append(resp)
        parsed = parse_plan_json(resp.text)
        if parsed is not None:
            subs = [s for s in parsed["subquestions"] if s.strip()][:max_sub]
            return Plan(route=parsed["route"], subquestions=subs, calls=calls, parsed=True)
    return Plan(route=ROUTE_SINGLE, subquestions=[], calls=calls,
                parsed=False, fallback_reason="plan_parse_failed")
