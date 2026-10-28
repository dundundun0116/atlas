"""双臂对照（A/B）与预注册判据。

用途：`docs/experiment-answer-format.md` 里的 H1/H2 判定不能事后凭感觉看。
把判据写成代码，跑完两边直接出结论，避免"看到数字再找解释"。

判据（与实验文档一致）：
* **H1（措辞问题）成立**：B 的严格 EM ≥ A + 0.08，且 B 的宽松 EM ≥ A - 0.02；
* **H2（能力问题）成立**：B 的严格 EM 提升 < 0.04，或 B 的宽松 EM 下降超过 0.02；
* 其余为**部分成立**，需要看逐题。
"""

from __future__ import annotations

from typing import Iterable

from .metrics import relaxed_em
from .report import summarize

STRICT_GAIN_H1 = 0.08
STRICT_GAIN_H2 = 0.04
RELAXED_TOL = 0.02


def arm_summary(rows: Iterable[dict]) -> dict:
    rows = [r for r in rows if r]
    s = summarize(rows)
    relaxed = sum(1 for r in rows if relaxed_em(r.get("pred", ""), r.get("gold", "")))
    n = len(rows) or 1
    return {
        "n": len(rows),
        "em_strict": s["quality"]["em"],
        "em_relaxed": relaxed / n,
        "f1": s["quality"]["f1"],
        "tokens_per_q": s["cost"]["tokens_per_q"],
        "refused_rate": s["reliability"]["refused_rate"],
    }


def judge_format_arm(base: dict, arm: dict, *,
                     strict_gain_h1: float = STRICT_GAIN_H1,
                     strict_gain_h2: float = STRICT_GAIN_H2,
                     relaxed_tol: float = RELAXED_TOL) -> dict:
    """按预注册判据判定 H1/H2。baseline 与 arm 都来自 `arm_summary`。"""
    d_strict = arm["em_strict"] - base["em_strict"]
    d_relaxed = arm["em_relaxed"] - base["em_relaxed"]
    if d_strict >= strict_gain_h1 and d_relaxed >= -relaxed_tol:
        verdict = "H1"
    elif d_strict < strict_gain_h2 or d_relaxed < -relaxed_tol:
        verdict = "H2"
    else:
        verdict = "partial"
    return {
        "verdict": verdict,
        "delta_strict": round(d_strict, 4),
        "delta_relaxed": round(d_relaxed, 4),
        "delta_f1": round(arm["f1"] - base["f1"], 4),
        "delta_tokens_per_q": round(arm["tokens_per_q"] - base["tokens_per_q"], 1),
        "note": {"H1": "措辞问题：约束把严格 EM 拉起来了",
                 "H2": "能力问题：约束没带来实质提升（或牺牲了宽松命中）",
                 "partial": "部分成立：看逐题再判断"}[verdict],
    }


__all__ = ["arm_summary", "judge_format_arm"]
