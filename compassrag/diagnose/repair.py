"""失败分类与有限修复（M2）。

五类失败里**只有两类需要模型**（`answer_unsupported` / `source_conflict`，来自 verifier），
其余三类都是**规则判定**：某轮检索没带来新证据（query miss）、证据不足且无方向
（evidence gap）、工具全挂（tool failure）。规则化的好处是便宜、可复现、可回放——
也才符合"LLM 提候选、确定性代码校验"的方法论。

修复有三条硬边界，缺一条就会变成无限烧预算：

1. 每类失败只对应**一个**动作（`REPAIR_FOR`）；
2. 全局最多修 `max_repairs` 次（默认 1）；
3. 用尽后一律 `REFUSE`，并让 agent 输出 `unknown` + `insufficient_evidence`，
   而不是把没有支撑的答案当成成功。
"""

from __future__ import annotations

from typing import Sequence

from ..agent.schemas import FailureClass, RepairAction, VerificationResult

MAX_REPAIRS = 1
BROKEN_OUTCOMES = {"error", "timeout", "invalid_params", "index_missing"}

# 失败 → 修复动作（终态动作只有 REFUSE）
REPAIR_FOR: dict[FailureClass, RepairAction] = {
    FailureClass.QUERY_MISS: RepairAction.REWRITE_QUERY,
    FailureClass.EVIDENCE_GAP: RepairAction.RETRIEVE_MORE,
    FailureClass.SOURCE_CONFLICT: RepairAction.SWITCH_VIEW,
    FailureClass.ANSWER_UNSUPPORTED: RepairAction.RETRIEVE_MORE,
    FailureClass.TOOL_FAILURE: RepairAction.SWITCH_VIEW,
}

# 固定顺序：轨迹与测试都依赖它稳定
_ORDER = [
    FailureClass.QUERY_MISS,
    FailureClass.EVIDENCE_GAP,
    FailureClass.SOURCE_CONFLICT,
    FailureClass.ANSWER_UNSUPPORTED,
    FailureClass.TOOL_FAILURE,
]


def classify_failures(*, tool_outcomes: Sequence[str] = (),
                      query_new_evidence: Sequence[int] = (),
                      verification: VerificationResult | None = None,
                      answer_empty: bool = False,
                      sufficiency_stuck: bool = False) -> list[FailureClass]:
    """把一次运行的事实翻成失败类别（只输出证据能支持的类别）。

    `query_new_evidence` 传**每条查询带来的新证据数**；判 QUERY_MISS 用的规则是
    "全部为 0"，而不是"存在 0"——多查询变体里个别变体重复属于正常现象。
    """
    found: set[FailureClass] = set()

    if tool_outcomes and all(o in BROKEN_OUTCOMES for o in tool_outcomes):
        found.add(FailureClass.TOOL_FAILURE)

    # 只有"所有查询都没带来新证据"才算 query miss：单个变体重复很常见，不该反复触发修复
    if query_new_evidence and all(n == 0 for n in query_new_evidence):
        found.add(FailureClass.QUERY_MISS)

    # 决策路径：判定"不够"却给不出下一条查询 ⇒ 没有可继续查的方向
    if sufficiency_stuck:
        found.add(FailureClass.EVIDENCE_GAP)
    # 有证据却答空，也是"证据没落到答案上"
    if answer_empty and query_new_evidence and any(n > 0 for n in query_new_evidence):
        found.add(FailureClass.EVIDENCE_GAP)

    if verification is not None and verification.parsed:
        if verification.source_conflict:
            found.add(FailureClass.SOURCE_CONFLICT)
        if not verification.claims or verification.unsupported > 0:
            found.add(FailureClass.ANSWER_UNSUPPORTED)

    return [f for f in _ORDER if f in found]


def plan_repair(failures: Sequence[FailureClass], *, repairs_used: int = 0,
                max_repairs: int = MAX_REPAIRS) -> RepairAction | None:
    """返回本次要执行的动作；`REFUSE` 表示修不了/修够了，应当拒答。"""
    if not failures:
        return None
    if repairs_used >= max(0, int(max_repairs)):
        return RepairAction.REFUSE
    for failure in failures:
        action = REPAIR_FOR.get(failure)
        if action is not None:
            return action
    return RepairAction.REFUSE


def choose_repair_query(action: RepairAction, *, claim_text: str = "",
                        next_query: str = "", question: str = "",
                        max_words: int = 15) -> str:
    """补检索用什么查询：**未支撑断言的原文**优先（它点名了缺的实体），其次判定器
    给的下一条查询，最后退回原问题。不额外调模型，保持确定性。"""
    if action is RepairAction.REFUSE:
        return ""
    for candidate in (claim_text, next_query, question):
        q = _as_query(candidate, max_words=max_words)
        if q:
            return q
    return ""


def _as_query(text: str, *, max_words: int) -> str:
    text = " ".join((text or "").strip().rstrip("?.").split())
    if not text:
        return ""
    words = text.split()
    return " ".join(words[:max_words])
