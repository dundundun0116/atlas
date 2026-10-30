"""报表层与 run manifest（M3）：同口径才可比，指纹不同就分开列。"""

import json

from compassrag.eval.manifest import (
    build_manifest,
    comparable,
    manifest_path,
    read_manifest,
    write_manifest,
)
from compassrag.eval.report import (
    format_table,
    load_rows,
    load_runs,
    report_run,
    stratified,
    summarize,
)


def _row(**kw):
    base = {
        "id": "q1", "pred": "Paris", "gold": "Paris", "em": 1.0, "f1": 1.0,
        "ev_recall": 0.5, "ev_full_hit": 0.0, "rounds": 2,
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "n_llm_calls": 3, "n_tool_calls": 2, "n_tool_failures": 0,
        "parse_fallbacks": 0, "stop_reason": "answered", "answer_empty": False,
        "refused": False, "claim_support_rate": 0.5, "stratum": "2",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 读数：作废口径必须默认排除
# --------------------------------------------------------------------------- #


def test_load_rows_skips_invalid_by_default(tmp_path):
    valid = tmp_path / "musique__naive.jsonl"
    invalid = tmp_path / "_invalid_musique__naive_budget512.jsonl"
    valid.write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    invalid.write_text(json.dumps(_row(em=0.0)) + "\n", encoding="utf-8")
    assert len(load_rows(valid)) == 1
    assert load_rows(invalid) == []
    assert len(load_rows(invalid, include_invalid=True)) == 1


def test_load_rows_tolerates_bad_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text(json.dumps(_row()) + "\n{broken\n\n", encoding="utf-8")
    assert len(load_rows(p)) == 1


def test_load_runs_filters_benchmark(tmp_path):
    (tmp_path / "musique__naive.jsonl").write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    (tmp_path / "2wiki__naive.jsonl").write_text(json.dumps(_row()) + "\n", encoding="utf-8")
    runs = load_runs(tmp_path)
    assert set(runs) == {"musique__naive", "2wiki__naive"}
    assert set(load_runs(tmp_path, benchmarks=["musique"])) == {"musique__naive"}


# --------------------------------------------------------------------------- #
# 四列摘要与分层
# --------------------------------------------------------------------------- #


def test_summarize_reports_four_columns():
    rows = [_row(), _row(id="q2", em=0.0, f1=0.0, ev_full_hit=1.0, stop_reason="early_stop",
                         total_tokens=80, n_llm_calls=2, rounds=1, refused=True)]
    s = summarize(rows)
    assert s["n"] == 2
    assert s["quality"]["em"] == 0.5
    assert s["evidence"]["ev_full_hit"] == 0.5
    assert s["cost"]["tokens_per_q"] == 100.0
    assert s["reliability"]["refused_rate"] == 0.5
    assert s["reliability"]["repair_success_rate"] == 0.0   # 没有任何行尝试过修复
    assert s["reliability"]["stop_reasons"] == {"answered": 1, "early_stop": 1}


def test_stratified_keeps_buckets_separate():
    rows = [_row(stratum="2"), _row(stratum="2", em=0.0), _row(stratum="4")]
    out = stratified(rows)
    assert set(out) == {"2", "4"}
    assert out["2"]["n"] == 2 and out["2"]["quality"]["em"] == 0.5
    assert out["4"]["n"] == 1


def test_format_table_and_report_render():
    rows = [_row()]
    table = format_table({"musique__naive": summarize(rows)})
    assert "musique__naive" in table and "EM" in table
    text = report_run("musique__naive", rows, stratify=True)
    assert "四" not in text and "停止原因" in text and "stratum=2" in text


def test_summarize_handles_empty_rows():
    s = summarize([])
    assert s["n"] == 0 and s["quality"]["em"] == 0.0


# --------------------------------------------------------------------------- #
# manifest：指纹决定可比性
# --------------------------------------------------------------------------- #


def test_manifest_roundtrip_and_stable_hash(tmp_path):
    kw = dict(repo=tmp_path, benchmark="musique", config_name="naive",
              config={"top_k": 10}, model="m", corpus_variant="samples",
              chunk_ids=["c2", "c1"], sample_file="f.jsonl", sample_ids=["q1", "q2"])
    m = build_manifest(**kw)
    # 集合指纹与顺序无关
    m2 = build_manifest(**{**kw, "chunk_ids": ["c1", "c2"]})
    assert m.corpus_hash == m2.corpus_hash
    path = write_manifest(m, manifest_path(tmp_path, "musique", "naive"))
    back = read_manifest(path)
    assert back is not None and back.fingerprint() == m.fingerprint()
    assert back.n_questions == 2 and back.corpus_chunks == 2


def test_manifest_fingerprint_changes_with_any_number_affecting_field(tmp_path):
    base = dict(repo=tmp_path, benchmark="musique", config_name="naive",
                config={"top_k": 10}, model="m", corpus_variant="samples",
                chunk_ids=["c1"], sample_ids=["q1"])
    m = build_manifest(**base)
    assert comparable(m, build_manifest(**base))
    assert not comparable(m, build_manifest(**{**base, "config": {"top_k": 5}}))
    assert not comparable(m, build_manifest(**{**base, "model": "other"}))
    assert not comparable(m, build_manifest(**{**base, "chunk_ids": ["c1", "c9"]}))
    assert not comparable(m, build_manifest(**{**base, "sample_ids": ["q9"]}))


# --------------------------------------------------------------------------- #
# plan accuracy 的弱代理指标（路由 vs 跳数）
# --------------------------------------------------------------------------- #


def test_plan_route_stats_uses_hop_as_weak_gold():
    from compassrag.eval.report import plan_route_stats

    rows = [
        _row(stratum="1", route="direct"),   # 对：1 跳允许 direct
        _row(stratum="2", route="multi"),    # 对
        _row(stratum="3", route="single"),   # 错
        _row(stratum="2", route=""),         # 无路由（scripted）→ 不进统计
    ]
    s = plan_route_stats(rows)
    assert s["n"] == 3
    assert abs(s["accuracy"] - 2 / 3) < 1e-9
    assert s["by_hop"]["2"]["n"] == 1 and s["by_hop"]["2"]["accuracy"] == 1.0   # 无路由的行不进统计


def test_plan_route_stats_empty_is_none():
    from compassrag.eval.report import plan_route_stats

    assert plan_route_stats([_row(route="")])["accuracy"] is None


# --------------------------------------------------------------------------- #
# 宽松命中只是诊断口径，不改主 EM
# --------------------------------------------------------------------------- #


def test_relaxed_em_recovers_formatting_variants():
    from compassrag.eval.metrics import relaxed_em

    assert relaxed_em("661 CE", "661") == 1.0
    assert relaxed_em("June 10, 1819", "10 June 1819") == 1.0
    assert relaxed_em("2013 (Silver)", "2013") == 1.0
    assert relaxed_em("British Landgrave Edmund Bellinger.", "Edmund Bellinger") == 1.0
    assert relaxed_em("Paris", "Lyon") == 0.0
    assert relaxed_em("", "Paris") == 0.0


def test_summarize_reports_relaxed_em_separately():
    rows = [_row(pred="661 CE", gold="661", em=0.0),
            _row(pred="Paris", gold="Paris", em=1.0)]
    s = summarize(rows)
    assert s["quality"]["em"] == 0.5          # 主口径不变
    assert s["quality"]["relaxed_em"] == 1.0  # 诊断口径把格式变体算回来


def test_format_table_includes_relaxed_em_column():
    table = format_table({"musique__naive": summarize([_row(pred="661 CE", gold="661", em=0.0)])})
    assert "宽松" in table
    assert "0.000" in table and "1.000" in table   # strict 0 / relaxed 1


def test_token_efficiency_is_em_per_1k_tokens():
    # EM 0.5、100 token/题 → 每 1000 token 换来 5.0 EM
    rows = [_row(em=1.0, prompt_tokens=50, completion_tokens=50, total_tokens=100),
            _row(em=0.0, prompt_tokens=50, completion_tokens=50, total_tokens=100)]
    s = summarize(rows)
    assert s["cost"]["tokens_per_q"] == 100
    assert s["cost"]["em_per_1k_tokens"] == 5.0


def test_zero_tokens_does_not_divide_by_zero():
    rows = [_row(total_tokens=0, prompt_tokens=0, completion_tokens=0)]
    assert summarize(rows)["cost"]["em_per_1k_tokens"] == 0.0
