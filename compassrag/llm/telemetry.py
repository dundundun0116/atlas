"""逐调用 token 遥测：追加 JSONL，按 (kind, tag, model) 聚合。"""

import json
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock


class Telemetry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def record(self, *, kind, tag, model, prompt_tokens=0, completion_tokens=0,
               total_tokens=0, latency_ms=0.0, n_items=None, error=None,
               finish_reason=None, reasoning_tokens=None) -> dict:
        row = {
            "ts": round(time.time(), 3),
            "kind": kind,
            "tag": tag,
            "model": model,
            "prompt_tokens": int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "latency_ms": round(float(latency_ms), 1),
        }
        if n_items is not None:
            row["n_items"] = int(n_items)
        # finish_reason=length 代表撞了预算上限（思维链吃光后可能出空答案）——诊断必需
        if finish_reason is not None:
            row["finish_reason"] = str(finish_reason)
        if reasoning_tokens is not None:
            row["reasoning_tokens"] = int(reasoning_tokens)
        if error is not None:
            row["error"] = str(error)[:300]
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def summarize(path) -> list[dict]:
    """按 (kind, tag, model) 聚合遥测行，供成本报表使用。"""
    agg = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0, "total": 0,
                               "lat": [], "n_items": 0})
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            a = agg[(r.get("kind"), r.get("tag"), r.get("model"))]
            a["calls"] += 1
            a["prompt"] += r.get("prompt_tokens", 0)
            a["completion"] += r.get("completion_tokens", 0)
            a["total"] += r.get("total_tokens", 0)
            a["lat"].append(r.get("latency_ms", 0.0))
            a["n_items"] += r.get("n_items", 0)
    out = []
    for (kind, tag, model), a in sorted(agg.items()):
        out.append({
            "kind": kind, "tag": tag, "model": model, "calls": a["calls"],
            "prompt_tokens": a["prompt"], "completion_tokens": a["completion"],
            "total_tokens": a["total"], "n_items_sum": a["n_items"],
            "avg_latency_ms": round(sum(a["lat"]) / len(a["lat"]), 1),
        })
    return out
