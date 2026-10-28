"""阶段 F 的离线 replay：一次运行的配置、步骤、上下文与报告都落盘。

验收要求"任意一次失败可回放"——这里保证：不调模型、只凭 `run.json` 就能重算指标并
定位到具体子任务。真实端点评测时，把同一结构里换成真实 step 即可。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from atlas.evaluation.harness import RunResult
from atlas.evaluation.metrics import aggregate, failing_runs, grouped_metrics, render_table

RUN_FILENAME = "run.json"


def save_run(directory: str | Path, config: Dict[str, Any],
             runs: Sequence[RunResult], extra: Dict[str, Any] | None = None) -> Path:
    """写出一份可离线回放的结果包。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / RUN_FILENAME
    payload: Dict[str, Any] = {
        "config": dict(config or {}),
        "runs": [run.to_dict() for run in runs],
    }
    if extra:
        payload["extra"] = dict(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_run(path: str | Path) -> Dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "runs" not in payload:
        raise ValueError(f"{path} 不是合法的 run 包：缺少 runs")
    return payload


def replay(path: str | Path) -> Dict[str, Any]:
    """从落盘结果重建 run 对象，给出指标、分组表与失败归因。"""
    payload = load_run(path)
    runs: List[RunResult] = [RunResult.from_dict(item) for item in payload["runs"]]
    return {
        "config": payload.get("config", {}),
        "metrics": aggregate(runs),
        "by_policy": grouped_metrics(runs, "policy"),
        "by_family": grouped_metrics(runs, "family"),
        "table": render_table(grouped_metrics(runs, "policy"), "按记忆策略"),
        "failures": failing_runs(runs),
        "runs": runs,
    }


def replay_metrics(path: str | Path) -> Dict[str, float]:
    return replay(path)["metrics"]


def replay_table(path: str | Path) -> str:
    return replay(path)["table"]
