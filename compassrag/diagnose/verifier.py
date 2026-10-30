"""claim 级证据核验（M2 的核心：把"证据够不够"落到可判定的断言上）。

为什么不让答案生成器一次性返回 claims：现在的答案调用是 SQuAD 短答口径
（"a few words"），一旦同一次调用还要产出 claims 与引用，**提示词与输出都变**，
五行消融表的 EM/F1 立刻失去可比性（历史行要重跑 ≈213 万 token/轮）。
所以这里是**独立的第二次调用**：答案不变，核验另算。

分工（项目方法论）：
* 模型只负责**提出** claim 与它认为的支撑证据编号；
* 编号是否真实存在、是否落在本次检索到的证据集合里，由**代码**判定；
* 模型说 supported 但引用为空/越界/不存在 ⇒ 一律记为不支持（reason 写明原因）。
"""

from __future__ import annotations

import json
import re
from typing import Sequence
from typing import Callable

from ..agent.schemas import Claim, VerificationResult

VERIFIER_BUDGETS = (1024, 2048, 4096)
VERIFIER_NUDGE = "Output the JSON now. Do not analyze further."

CLAIM_PROMPT = """You check whether an answer is supported by the evidence passages.

Rules:
- Split the answer into the minimal claims needed to state it (a few words each).
- For each claim, list the evidence numbers that literally support it (1-based, as shown in [n]).
- supported=true ONLY if at least one listed passage literally contains the claim's fact.
- If two passages give different values for the same fact, set "source_conflict": true.
- Do not use outside knowledge. If a claim is not in the passages, supported=false and cite nothing.

Output JSON only, no other text:
{{"claims": [{{"text": "...", "evidence": [1], "supported": true}}], "source_conflict": false}}

Question: {question}
Answer: {answer}

Evidence:
{evidence}"""


def parse_claims_json(text: str) -> dict | None:
    """容错解析：去 ``` 围栏、截取第一个 JSON 对象；形状不合法返回 None。"""
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
    if not isinstance(obj, dict) or not isinstance(obj.get("claims"), list):
        return None
    claims = []
    for item in obj["claims"]:
        if not isinstance(item, dict):
            continue
        txt = str(item.get("text", "")).strip()
        if not txt:
            continue
        refs = item.get("evidence", [])
        if not isinstance(refs, list):
            refs = []
        nums = [int(n) for n in refs if isinstance(n, (int, float)) and not isinstance(n, bool)]
        claims.append({
            "text": txt,
            "evidence": [n for n in nums if n > 0],
            "supported": bool(item.get("supported", False)),
        })
    return {"claims": claims, "source_conflict": bool(obj.get("source_conflict", False)),
            "conflict_note": str(obj.get("conflict_note", "") or "")[:200]}


def resolve_claims(raw: dict, evidence_ids: Sequence[str]) -> list[Claim]:
    """把模型给的证据编号换成 chunk id；编号越界/引空 ⇒ 该 claim 记为不支持。"""
    out: list[Claim] = []
    for item in raw["claims"]:
        nums = item["evidence"]
        picked = [evidence_ids[n - 1] for n in nums if 1 <= n <= len(evidence_ids)]
        bad = [n for n in nums if not (1 <= n <= len(evidence_ids))]
        cites = item["supported"] and bool(picked) and not bad
        reason = ""
        if bad:
            reason = f"引用了不存在的证据编号 {bad}"
        elif item["supported"] and not nums:
            reason = "声称有支撑但未给出引用"
        elif not item["supported"]:
            reason = "模型判定无支撑"
        out.append(Claim(text=item["text"], evidence_ids=picked, supported=cites, reason=reason))
    return out


def verify_answer(llm, question: str, answer: str, evidence: Sequence[tuple[str, str]],
                  *, budgets: tuple = VERIFIER_BUDGETS,
                  cancel_check: Callable[[], bool] | None = None) -> tuple[VerificationResult, list]:
    """核验一次答案；返回 (结果, 消耗的 LLM 调用)。evidence 为 [(chunk_id, 文本)]。"""
    if not evidence:
        # 没有证据时无需调模型：任何断言都不可能被支持
        return VerificationResult(parsed=True), []

    rendered = "\n".join(f"[{i}] {text}" for i, (_, text) in enumerate(evidence, start=1))
    ids = [cid for cid, _ in evidence]
    messages = [
        {"role": "system", "content": "You verify claims against evidence passages."},
        {"role": "user", "content": CLAIM_PROMPT.format(question=question, answer=answer or "(empty)",
                                                        evidence=rendered)},
    ]
    calls: list = []
    for n, budget in enumerate(budgets):
        if cancel_check is not None and cancel_check():
            break
        msgs = messages if n == 0 else messages + [{"role": "user", "content": VERIFIER_NUDGE}]
        resp = llm.chat(msgs, tag="verify" if n == 0 else "verify_retry",
                        max_tokens=int(budget))
        calls.append(resp)
        raw = parse_claims_json(resp.text)
        if raw is not None:
            claims = resolve_claims(raw, ids)
            return VerificationResult(
                claims=claims, parsed=True,
                source_conflict=raw["source_conflict"],
                conflict_note=raw["conflict_note"]), calls
    # 解析全败：不触发修复（否则会把模型不稳定当成证据问题），只标记 parsed=False
    return VerificationResult(parsed=False), calls
