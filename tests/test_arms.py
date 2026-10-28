"""双臂对照判据：H1/H2 的阈值行为必须可预测。"""

from compassrag.eval.arms import arm_summary, judge_format_arm


def _sum(em, relaxed, f1=0.5, tok=1000.0, refused=0.0):
    return {"n": 50, "em_strict": em, "em_relaxed": relaxed, "f1": f1,
            "tokens_per_q": tok, "refused_rate": refused}


def test_h1_when_strict_recovers_and_relaxed_holds():
    r = judge_format_arm(_sum(0.46, 0.60), _sum(0.60, 0.60))
    assert r["verdict"] == "H1" and r["delta_strict"] == 0.14


def test_h2_when_gain_too_small():
    r = judge_format_arm(_sum(0.46, 0.60), _sum(0.48, 0.60))
    assert r["verdict"] == "H2"


def test_h2_when_relaxed_drops():
    r = judge_format_arm(_sum(0.46, 0.60), _sum(0.56, 0.55))
    assert r["verdict"] == "H2"


def test_partial_in_between():
    r = judge_format_arm(_sum(0.46, 0.60), _sum(0.52, 0.59))
    assert r["verdict"] == "partial"


def test_arm_summary_reports_both_metrics():
    rows = [{"pred": "661 CE", "gold": "661", "em": 0.0, "f1": 0.0,
             "ev_recall": 1.0, "total_tokens": 100, "stop_reason": "answered",
             "prompt_tokens": 100, "completion_tokens": 0, "rounds": 1,
             "answer_empty": False, "refused": False}]
    s = arm_summary(rows)
    assert s["em_strict"] == 0.0 and s["em_relaxed"] == 1.0
    assert s["tokens_per_q"] == 100
