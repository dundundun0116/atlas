"""轨迹落盘与回放（M0；M3 评测升级的地基）。

**两层落盘，不要混成一层：**

* `trace`——一题的完整事实（每步 plan/retrieve/verify/answer、每条查询来源、每次工具
  调用的 retriever/rank/outcome、证据 id 与标题、token、停止原因）。JSONL，一行一题。
* `row`——由 trace **派生**出来的指标行（em / f1 / 证据召回 / full-hit / 轮数 / token）。
  只存结论，不存事实。

这样做的理由（实测成本）：五配置各跑 50 题 ≈ 213 万 token/轮。一旦发现"指标少了一列"，
有 trace 就本地重算，没有就得整轮重跑。所以 trace 必须一次定对、且写全。

回放 = 读 trace 重新算派生指标，**不重新请求模型**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..agent.schemas import AnswerTrace
from .metrics import em_score, f1_score, full_hit_at_k, recall_at_k

SCHEMA_VERSION = 1
TRACE_DIR_DEFAULT = Path("runs") / "traces"


class TraceWriter:
    """按 run 追加写 trace；同日同 run 多次评测不会互相覆盖。"""

    def __init__(self, path: str | Path, *, append: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mode = "a" if append else "w"

    def append(self, trace: AnswerTrace) -> Path:
        with self.path.open(self._mode, encoding="utf-8") as f:
            f.write(trace.model_dump_json() + "\n")
        self._mode = "a"
        return self.path

    def extend(self, traces: Iterable[AnswerTrace]) -> Path:
        for t in traces:
            self.append(t)
        return self.path


def write_traces(traces: Iterable[AnswerTrace], path: str | Path, *,
                 append: bool = True) -> Path:
    return TraceWriter(path, append=append).extend(traces)


def iter_traces(path: str | Path) -> Iterator[AnswerTrace]:
    """逐行读回；坏行跳过而不是让整个文件读不出来。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield AnswerTrace.model_validate_json(line)
            except Exception:  # noqa: BLE001 - 单行损坏不该毁掉整批复算
                continue


def trace_path(run_dir: str | Path, bench: str, config: str) -> Path:
    return Path(run_dir) / f"{bench}__{config}.jsonl"


# --------------------------------------------------------------------------- #
# 回放：从 trace 派生指标（不碰模型）
# --------------------------------------------------------------------------- #


def derive_row(trace: AnswerTrace, *, gold: str | None = None,
               gold_titles: set[str] | None = None,
               question_id: str | None = None,
               stratum: str | None = None,
               k: int | None = None) -> dict[str, Any]:
    """把一条 trace 变成评测表的一行。所有字段都能从 trace 重算。"""
    top_k = k or (max((h.rank for c in trace.tool_calls for h in c.hits), default=0) or len(trace.evidence_ids) or 1)
    row: dict[str, Any] = {
        "id": question_id or trace.run_id,
        "stratum": stratum,
        "pred": trace.answer,
        "gold": gold,
        "route": trace.route.value,
        "stop_reason": trace.stop_reason.value,
        "rounds": trace.rounds,
        "n_queries": len(trace.queries),
        "queries": [q.query for q in trace.queries],
        "query_sources": [q.source.value for q in trace.queries],
        "n_tool_calls": len(trace.tool_calls),
        "n_tool_failures": sum(1 for c in trace.tool_calls if not c.ok),
        "n_llm_calls": trace.n_llm_calls,
        "n_answer_calls": trace.n_answer_calls,
        "parse_fallbacks": trace.parse_fallbacks,
        "prompt_tokens": trace.usage.prompt_tokens,
        "completion_tokens": trace.usage.completion_tokens,
        "total_tokens": trace.usage.total,
        "answer_empty": not trace.answer.strip(),
        "evidence_ids": list(trace.evidence_ids),
        "evidence_titles": list(trace.evidence_titles),
        "trace_latency_ms": trace.latency_ms,
        "schema_version": trace.schema_version,
        "n_calls": trace.n_answer_calls,
        "finish_reasons": _answer_finish_reasons(trace),
    }
    # ---- 证据闭环（M2）----
    v = trace.verification
    n_claims = len(v.claims) if v is not None else 0
    supported = sum(1 for c in v.claims if c.supported) if v is not None else 0
    row.update({
        "verified": v is not None,
        "n_claims": n_claims,
        "unsupported_claims": v.unsupported if v is not None else 0,
        "claim_support_rate": (supported / n_claims) if n_claims else None,
        "source_conflict": bool(v.source_conflict) if v is not None else False,
        "failures": [f.value for f in trace.failures],
        "repairs": [r.value for r in trace.repairs],
        # 修复是否真的把问题修掉了：尝试过修复、最终无失败类别、且没有退化成拒答
        "repair_success": (bool(trace.repairs) and not trace.failures
                           and (trace.answer or "").strip().lower() != "unknown")
        if trace.repairs else None,
        "refused": (trace.answer or "").strip().lower() == "unknown",
    })
    if gold is not None:
        row["em"] = em_score(trace.answer, gold)
        row["f1"] = f1_score(trace.answer, gold)
    if gold_titles:
        titles = trace.evidence_titles or trace.evidence_ids
        row["ev_recall"] = recall_at_k(titles, gold_titles, k=top_k)
        row["ev_full_hit"] = full_hit_at_k(titles, gold_titles, k=top_k)
    return row


def _answer_finish_reasons(trace: AnswerTrace) -> list[str]:
    for step in trace.steps:
        if step.kind == "answer":
            return list(step.detail.get("finish_reasons") or [])
    return []


def replay_rows(path: str | Path, *, gold_by_id: dict[str, str] | None = None,
                titles_by_id: dict[str, set[str]] | None = None,
                stratum_by_id: dict[str, str] | None = None,
                k: int | None = None) -> list[dict[str, Any]]:
    """回放一个 trace 文件：给了金标就顺带重算 EM/F1/证据指标。"""
    out: list[dict[str, Any]] = []
    for trace in iter_traces(path):
        qid = _question_id_of(trace)
        out.append(derive_row(
            trace,
            gold=(gold_by_id or {}).get(qid) if gold_by_id else None,
            gold_titles=(titles_by_id or {}).get(qid) if titles_by_id else None,
            question_id=qid, stratum=(stratum_by_id or {}).get(qid) if stratum_by_id else None,
            k=k,
        ))
    return out


def _question_id_of(trace: AnswerTrace) -> str | None:
    """trace 里没有题库 id 时退回 None（调用方可用 run_id 关联）。"""
    return trace.config.get("question_id") if trace.config else None


def aggregate(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """聚合派生行：质量、证据、成本、可靠性四列一起给。"""
    rows = [r for r in rows if r]
    n = len(rows) or 1
    stops: dict[str, int] = {}
    for r in rows:
        stops[r.get("stop_reason", "")] = stops.get(r.get("stop_reason", ""), 0) + 1
    return {
        "n": len(rows),
        "em": sum(r.get("em", 0.0) for r in rows) / n,
        "f1": sum(r.get("f1", 0.0) for r in rows) / n,
        "ev_recall": sum(r.get("ev_recall", 0.0) for r in rows) / n,
        # 旧格式行没有 full-hit（无证据标题），只能在"有该字段的行"上取均值
        "ev_full_hit": _mean([r["ev_full_hit"] for r in rows if r.get("ev_full_hit") is not None]),
        "empty_rate": sum(1 for r in rows if r.get("answer_empty")) / n,
        "tokens_per_q": sum(_tokens(r) for r in rows) / n,
        "llm_calls_per_q": sum(r.get("n_llm_calls", 0) for r in rows) / n,
        "rounds_per_q": sum(r.get("rounds", 0) for r in rows) / n,
        "tool_failure_rate": sum(1 for r in rows if r.get("n_tool_failures")) / n,
        "parse_fallback_rate": sum(1 for r in rows if r.get("parse_fallbacks")) / n,
        "early_stop_rate": sum(1 for r in rows if r.get("stop_reason") == "early_stop") / n,
        "refused_rate": sum(1 for r in rows if r.get("refused")) / n,
        "verified_rate": sum(1 for r in rows if r.get("verified")) / n,
        "claim_support_rate": _mean([r["claim_support_rate"] for r in rows
                                     if r.get("claim_support_rate") is not None]),
        "repair_rate": sum(1 for r in rows if r.get("repairs")) / n,
        "repair_success_rate": _mean([1.0 if r.get("repair_success") else 0.0 for r in rows
                                      if r.get("repair_success") is not None]),
        "stop_reasons": dict(sorted(stops.items())),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _tokens(row: dict) -> int:
    """优先 total_tokens；旧行只有 prompt/completion，按两者之和算。"""
    total = row.get("total_tokens")
    if total:
        return int(total)
    return int(row.get("prompt_tokens", 0) or 0) + int(row.get("completion_tokens", 0) or 0)
