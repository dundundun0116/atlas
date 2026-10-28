"""证据充分度判定 + 下一步查询（① 的循环内决策）。

一次调用同时回答两件事：现有证据能否作答？若不能，下一轮该查什么。
解析失败一律降级为"充分"（停止循环）——宁可少查一轮，也不无限烧预算。
"""

import json
import re
from dataclasses import dataclass, field

SUFFICIENCY_NUDGE = "Output the JSON now. Do not analyze further."
SUFFICIENCY_BUDGETS = (1024, 2048, 4096)

SUFFICIENCY_PROMPT = """You check whether the evidence is sufficient to answer the question.

Output JSON only, no other text:
{{"sufficient": true/false, "next_query": "<one retrieval query for the missing information, or empty string>"}}

Rules:
- "sufficient": true only if the evidence alone supports a definite answer.
- If not sufficient, "next_query" must be a single self-contained search query, under 15 words,
  naming the missing entities explicitly (keyword-like; long natural-language questions retrieve badly).

Question: {question}

Evidence:
{evidence}"""


@dataclass
class Sufficiency:
    ok: bool = True
    next_query: str = ""
    calls: list = field(default_factory=list)   # 本次决策消耗的 LLM 调用（供成本聚合）
    parsed: bool = True                         # False = 解析全败后的安全降级（判为"充分"）


def parse_sufficiency_json(text: str) -> dict | None:
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
    if not isinstance(obj, dict) or not isinstance(obj.get("sufficient"), bool):
        return None
    nq = obj.get("next_query", "")
    return {"sufficient": obj["sufficient"], "next_query": nq.strip() if isinstance(nq, str) else ""}


def check_sufficiency(llm, question: str, evidence_text: str,
                      *, budgets: tuple = SUFFICIENCY_BUDGETS) -> Sufficiency:
    messages = [{"role": "system", "content": "You judge evidence sufficiency for question answering."},
                {"role": "user", "content": SUFFICIENCY_PROMPT.format(question=question,
                                                                      evidence=evidence_text)}]
    calls: list = []
    for n, budget in enumerate(budgets):
        msgs = messages if n == 0 else messages + [{"role": "user", "content": SUFFICIENCY_NUDGE}]
        resp = llm.chat(msgs, tag="sufficiency" if n == 0 else "sufficiency_retry",
                        max_tokens=int(budget))
        calls.append(resp)
        parsed = parse_sufficiency_json(resp.text)
        if parsed is not None:
            return Sufficiency(ok=parsed["sufficient"], next_query=parsed["next_query"],
                               calls=calls, parsed=True)
    # 解析全败 → 降级为"充分"（停止循环，宁可少查一轮也不无限烧预算）
    return Sufficiency(ok=True, next_query="", calls=calls, parsed=False)
