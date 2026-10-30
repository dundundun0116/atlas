"""报表层（M3）：把 `runs/` 读成**一张四列表**——质量 / 证据 / 成本 / 可靠性。

一条规矩：**同一张表里的行必须同口径**。所以这里默认
* 跳过 `_invalid_` 前缀的作废口径文件（`lessons/L3`）；
* 按基准分开报——`stratum` 的语义随基准变（`lessons/E1`），三个基准的分层数字
  不能混在同一张图里；
* 每个 run 的 manifest 指纹不同的，宁可分开列，也不要平均。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .metrics import relaxed_em
from .traces import aggregate

INVALID_PREFIX = "_invalid_"


def load_rows(path: str | Path, *, include_invalid: bool = False) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    if not include_invalid and p.name.startswith(INVALID_PREFIX):
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_runs(qa_dir: str | Path, *, include_invalid: bool = False,
              benchmarks: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """{run 名: 行}；run 名即文件名去掉 .jsonl。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(Path(qa_dir).glob("*.jsonl")):
        if not include_invalid and p.name.startswith(INVALID_PREFIX):
            continue
        if benchmarks and not any(p.name.startswith(f"{b}__") for b in benchmarks):
            continue
        rows = load_rows(p, include_invalid=include_invalid)
        if rows:
            out[p.stem] = rows
    return out


def _hop(row: dict[str, Any]) -> int | None:
    """MuSiQue 的 stratum 是支撑段落数，可当跳数的弱标注；其它基准不可这么用（lessons/E1）。"""
    try:
        return int(str(row.get("stratum", "")).strip())
    except (TypeError, ValueError):
        return None


def plan_route_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """plan accuracy 的**可得代理指标**：路由是否与跳数匹配。

    MuSiQue 没有人工标注的"检索计划金标"，但有跳数（支撑段落数）。所以这里用一条
    弱规则当金标：1 跳的问题应当 `single`（或 `direct`），≥2 跳应当 `multi`。
    它只能说明"路由有没有离谱"，不能说明"计划质量"——报告里必须这么写。
    """
    pairs: list[tuple[int, str, bool]] = []
    for r in rows:
        hop = _hop(r)
        route = str(r.get("route") or "").strip()
        if hop is None or route not in ("direct", "single", "multi"):
            continue
        expected = {"direct", "single"} if hop <= 1 else {"multi"}
        pairs.append((hop, route, route in expected))
    if not pairs:
        return {"n": 0, "accuracy": None, "by_hop": {}}
    by_hop: dict[str, dict[str, Any]] = {}
    for hop, _, ok in pairs:
        b = by_hop.setdefault(str(hop), {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += int(ok)
    return {
        "n": len(pairs),
        "accuracy": sum(1 for *_, ok in pairs if ok) / len(pairs),
        "by_hop": {k: {"n": v["n"], "accuracy": v["correct"] / v["n"]}
                   for k, v in sorted(by_hop.items())},
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """四列摘要；缺字段的行（旧格式）按 0 处理，不让整张表挂掉。"""
    agg = aggregate(rows)
    return {
        "n": agg["n"],
        "quality": {"em": agg["em"], "f1": agg["f1"], "empty_rate": agg["empty_rate"],
                    "relaxed_em": _mean_value([relaxed_em(r.get("pred", ""), r.get("gold", ""))
                                               for r in rows if r.get("gold")])},
        "evidence": {"ev_recall": agg["ev_recall"],
                     "ev_full_hit": agg["ev_full_hit"] if any("ev_full_hit" in r for r in rows) else None,
                     "claim_support_rate": agg.get("claim_support_rate", 0.0)
                     if any("claim_support_rate" in r for r in rows) else None},
        "cost": {"em_per_1k_tokens": (agg["em"] / (agg["tokens_per_q"] / 1000.0)
                                      if agg["tokens_per_q"] else 0.0),
                 "tokens_per_q": agg["tokens_per_q"],
                 "llm_calls_per_q": agg["llm_calls_per_q"],
                 "rounds_per_q": agg["rounds_per_q"]},
        "plan": plan_route_stats(rows),
        "reliability": {"tool_failure_rate": agg["tool_failure_rate"],
                        "parse_fallback_rate": agg["parse_fallback_rate"],
                        "refused_rate": agg.get("refused_rate", 0.0),
                        "repair_success_rate": agg.get("repair_success_rate", 0.0),
                        "early_stop_rate": agg["early_stop_rate"],
                        "stop_reasons": agg["stop_reasons"]},
    }


def stratified(rows: list[dict[str, Any]], key: str = "stratum") -> dict[str, dict[str, Any]]:
    """按分层键拆分后各自摘要（同一基准内部用；跨基准不要合并）。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(str(r.get(key) or "unknown"), []).append(r)
    return {k: summarize(v) for k, v in sorted(buckets.items())}


def format_table(summaries: dict[str, dict[str, Any]]) -> str:
    header = (f"{'run':<34} {'n':>4} {'EM':>6} {'宽松':>6} {'F1':>6} {'召回':>6} {'full':>6} "
              f"{'claim':>6} {'tok/q':>7} {'轮':>5} {'拒答':>6} {'工具败':>7}")
    lines = [header, "-" * len(header)]
    for name, s in summaries.items():
        q, e, c, r = s["quality"], s["evidence"], s["cost"], s["reliability"]
        full = "-" if e["ev_full_hit"] is None else f"{e['ev_full_hit']:.3f}"
        claim = "-" if e["claim_support_rate"] is None else f"{e['claim_support_rate']:.3f}"
        lines.append(
            f"{name:<34} {s['n']:>4} {q['em']:>6.3f} {q.get('relaxed_em', 0.0):>6.3f} {q['f1']:>6.3f} "
            f"{e['ev_recall']:>6.3f} {full:>6} {claim:>6} "
            f"{c['tokens_per_q']:>7.0f} {c['rounds_per_q']:>5.2f} {r['refused_rate']:>6.3f} "
            f"{r['tool_failure_rate']:>7.3f}")
    return "\n".join(lines)


def report_run(name: str, rows: list[dict[str, Any]], *, stratify: bool = False) -> str:
    """单个 run 的人读报告：四列 + 停止原因分布（+ 可选分层）。"""
    s = summarize(rows)
    parts = [f"# {name}", "",
             f"样本 {s['n']} 题 ｜ EM {s['quality']['em']:.3f} ｜ F1 {s['quality']['f1']:.3f}"
             f" ｜ 宽松命中(诊断) {s['quality']['relaxed_em']:.3f}",
             f"证据 召回 {s['evidence']['ev_recall']:.3f} ｜ full-hit {_fmt(s['evidence']['ev_full_hit'])}"
             f" ｜ claim support {_fmt(s['evidence']['claim_support_rate'])}",
             f"成本 {s['cost']['tokens_per_q']:.0f} token/题 ｜ "
             f"{s['cost']['llm_calls_per_q']:.2f} 次调用/题 ｜ {s['cost']['rounds_per_q']:.2f} 轮/题",
             f"可靠性 工具失败 {s['reliability']['tool_failure_rate']:.3f} ｜ "
             f"解析降级 {s['reliability']['parse_fallback_rate']:.3f} ｜ "
             f"拒答 {s['reliability']['refused_rate']:.3f}",
             f"停止原因 {s['reliability']['stop_reasons']}",
             f"路由-跳数一致率 {_fmt(s['plan']['accuracy'])}（n={s['plan']['n']}，弱金标）", ""]
    if stratify:
        for bucket, bs in stratified(rows).items():
            parts.append(f"- stratum={bucket}: n={bs['n']} EM={bs['quality']['em']:.3f} "
                         f"F1={bs['quality']['f1']:.3f} full-hit={_fmt(bs['evidence']['ev_full_hit'])}")
    return "\n".join(parts)


def _mean_value(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def comparability_warnings(runs: dict[str, list[dict]]) -> list[str]:
    """同一基准内样本量不同 ⇒ 这两行不可直接比较。"""
    seen: dict[str, set[int]] = {}
    for name, rows in runs.items():
        bench = name.split("__")[0]
        seen.setdefault(bench, set()).add(len(rows))
    return [f"{b}: 样本量不一致 {sorted(ns)}，这些行不能直接对比"
            for b, ns in seen.items() if len(ns) > 1]
