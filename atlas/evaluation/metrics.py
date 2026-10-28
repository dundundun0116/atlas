"""阶段 F 指标：一张表同时给出任务质量、可靠性、上下文与成本。

口径固定，避免"换个分母就变好看"：
- 完成率 = 整条任务的终态是否 completed（不是子任务平均）；
- 约束违规率 / 过期误用率 = 只要该 run 里有一步发生就算一次（保守口径）；
- 恢复成功率 = 被中断的 run 中恢复并完成的占比；没有中断时记为 1.0 并单列 `interrupted_runs`，
  不允许"没有样本却宣称 100%"。
"""

from typing import Dict, Iterable, List, Sequence

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


def aggregate(runs: Sequence[RunResult]) -> Dict[str, float]:
    """把一组 run 压成指标字典；空集合返回全 0，不抛异常。"""
    interrupted = [r for r in runs if r.interrupted]
    return {
        "n": len(runs),
        "interrupted_runs": len(interrupted),
        "completion_rate": _mean([1.0 if r.completed else 0.0 for r in runs]),
        "constraint_violation_rate": _mean([1.0 if r.violations else 0.0 for r in runs]),
        "stale_misuse_rate": _mean([1.0 if r.stale_misuses else 0.0 for r in runs]),
        "recovery_success_rate": (
            _mean([1.0 if r.recovered else 0.0 for r in interrupted]) if interrupted else 1.0
        ),
        "avg_context_tokens": _mean([r.context_tokens for r in runs]),
        "avg_model_calls": _mean([r.model_calls for r in runs]),
        "avg_latency_ms": _mean([r.latency_ms for r in runs]),
        "avg_cost_usd": round(_mean([r.cost_usd for r in runs]), 6),
    }


def group_by(runs: Sequence[RunResult], key: str) -> Dict[str, List[RunResult]]:
    groups: Dict[str, List[RunResult]] = {}
    for run in runs:
        groups.setdefault(str(getattr(run, key)), []).append(run)
    return dict(sorted(groups.items()))


def grouped_metrics(runs: Sequence[RunResult], key: str = "policy") -> Dict[str, Dict[str, float]]:
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
    ("延迟ms", 8),
    ("成本USD", 9),
)


def render_table(groups: Dict[str, Dict[str, float]], title: str = "") -> str:
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
                    f"{row.get('completion_rate', 0):.3f}",
                    f"{row.get('constraint_violation_rate', 0):.3f}",
                    f"{row.get('stale_misuse_rate', 0):.3f}",
                    f"{row.get('recovery_success_rate', 0):.3f}",
                    f"{row.get('avg_context_tokens', 0):.1f}",
                    f"{row.get('avg_model_calls', 0):.1f}",
                    f"{row.get('avg_latency_ms', 0):.1f}",
                    f"{row.get('avg_cost_usd', 0):.4f}",
                ]
            )
            + " |"
        )
    return "\n".join(line for line in lines if line)


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
