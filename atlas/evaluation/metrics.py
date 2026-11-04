"""阶段 F 指标：一张表同时给出任务质量、可靠性、上下文与成本。

口径固定，避免"换个分母就变好看"：
- 完成率 = 整条任务的终态是否 completed（不是子任务平均）；
- 约束违规率 / 过期误用率 = 只要该 run 里有一步发生就算一次（保守口径）；
- **恢复成功率：没有故障注入样本时返回 `None`**，不再写 1.0。设计文档 §10.3
  与调研报告 §7.6 都要求"无样本报 null"——没有中断样本就宣称 100% 是过度宣称。
- 延迟/成本带来源标记（`latency_source` / `cost_source`）：当前评测脚手架里它们是
  按 token 数估算的 `synthetic_estimate`，不是实测值；表尾会如实标注。
"""

from typing import Any, Dict, Iterable, List, Sequence

from atlas.evaluation.harness import RunResult

REQUIRED_METRICS = (
    "completion_rate",
    "constraint_violation_rate",
    "stale_misuse_rate",
    "recovery_success_rate",
    "avg_context_tokens",
    "avg_model_calls",
    "avg_latency_ms",
    "avg_cost_usd",
)


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _source(values: Iterable[str]) -> str:
    """一组指标值的来源：全一致就报那个来源，否则 mixed。"""
    unique = sorted({str(v) for v in values if str(v or "").strip()})
    if not unique:
        return "unknown"
    return unique[0] if len(unique) == 1 else "mixed"


def aggregate(runs: Sequence[RunResult]) -> Dict[str, Any]:
    """把一组 run 压成指标字典；空集合返回 0 计数的字典，不抛异常。"""
    interrupted = [r for r in runs if r.interrupted]
    return {
        "n": len(runs),
        "interrupted_runs": len(interrupted),
        "completion_rate": _mean([1.0 if r.completed else 0.0 for r in runs]),
        "constraint_violation_rate": _mean([1.0 if r.violations else 0.0 for r in runs]),
        "stale_misuse_rate": _mean([1.0 if r.stale_misuses else 0.0 for r in runs]),
        # 没有故障注入样本 ⇒ null（None），不是 1.0
        "recovery_success_rate": (
            _mean([1.0 if r.recovered else 0.0 for r in interrupted]) if interrupted else None
        ),
        "avg_context_tokens": _mean([r.context_tokens for r in runs]),
        "avg_model_calls": _mean([r.model_calls for r in runs]),
        "avg_latency_ms": _mean([r.latency_ms for r in runs]),
        "avg_cost_usd": round(_mean([r.cost_usd for r in runs]), 6),
        # 指标来源：从估算值变实测值时，这里会自动从 synthetic_estimate 变成 measured
        "latency_source": _source(getattr(r, "latency_source", "") for r in runs),
        "cost_source": _source(getattr(r, "cost_source", "") for r in runs),
    }


def group_by(runs: Sequence[RunResult], key: str) -> Dict[str, List[RunResult]]:
    groups: Dict[str, List[RunResult]] = {}
    for run in runs:
        groups.setdefault(str(getattr(run, key)), []).append(run)
    return dict(sorted(groups.items()))


def grouped_metrics(runs: Sequence[RunResult], key: str = "policy") -> Dict[str, Dict[str, Any]]:
    return {name: aggregate(group) for name, group in group_by(runs, key).items()}


_COLUMNS = (
    ("组", 18),
    ("n", 4),
    ("完成率", 8),
    ("约束违规率", 10),
    ("过期误用率", 10),
    ("恢复成功率", 10),
    ("上下文token", 11),
    ("调用次数", 8),
    ("延迟ms(估算)", 12),
    ("成本USD", 9),
)


def _format_rate(value: Any) -> str:
    """无样本的指标显示 `—`，而不是 0 或 1（两者都是无根据的断言）。"""
    return "—" if value is None else f"{float(value):.3f}"


def render_table(groups: Dict[str, Dict[str, Any]], title: str = "") -> str:
    """Markdown 表：质量 / 可靠性 / 上下文 / 成本 四类同表，组名按字典序。"""
    header = "| " + " | ".join(name for name, _ in _COLUMNS) + " |"
    divider = "|" + "|".join("---" for _ in _COLUMNS) + "|"
    lines = [f"### {title}" if title else "", header, divider]
    for name in sorted(groups):
        row = groups[name]
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    str(int(row.get("n", 0))),
                    _format_rate(row.get("completion_rate", 0)),
                    _format_rate(row.get("constraint_violation_rate", 0)),
                    _format_rate(row.get("stale_misuse_rate", 0)),
                    _format_rate(row.get("recovery_success_rate")),
                    f"{row.get('avg_context_tokens', 0):.1f}",
                    f"{row.get('avg_model_calls', 0):.1f}",
                    f"{row.get('avg_latency_ms', 0):.1f}",
                    f"{row.get('avg_cost_usd', 0):.4f}",
                ]
            )
            + " |"
        )
    notes = _footnotes(groups)
    return "\n".join(line for line in lines + notes if line)


def _footnotes(groups: Dict[str, Dict[str, Any]]) -> List[str]:
    """如实标注口径：哪些指标是估算、哪些指标没有样本。"""
    notes: List[str] = []
    sources = {
        str(row.get("latency_source", "")) for row in groups.values()
    } | {str(row.get("cost_source", "")) for row in groups.values()}
    if any("synthetic" in source for source in sources):
        notes.append("")
        notes.append("> 延迟/成本为 `synthetic_estimate`（按 token 数与单价估算），不是实测值。")
    if any(row.get("recovery_success_rate") is None for row in groups.values()):
        notes.append("> 恢复成功率为 `—`：该组没有故障注入样本（无样本报 null，不报 1.0）。")
    return notes


def failing_runs(runs: Iterable[RunResult]) -> List[Dict[str, object]]:
    """失败归因：哪条 run 的哪一步违规/误用/报错（replay 与排障共用）。"""
    findings: List[Dict[str, object]] = []
    for run in runs:
        for step in run.steps:
            if step.completed and not step.violation and not step.stale_misuse and not step.error:
                continue
            findings.append(
                {
                    "task_id": run.task_id,
                    "policy": run.policy,
                    "subtask_id": step.subtask_id,
                    "completed": step.completed,
                    "violation": step.violation,
                    "stale_misuse": step.stale_misuse,
                    "error": step.error,
                    "context_tokens": step.context_tokens,
                }
            )
    return findings
