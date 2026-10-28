"""trace 存储：内存 + 可选落盘，供 `GET /runs/{trace_id}` 回读。"""

from __future__ import annotations

import json
from pathlib import Path

from ..agent.schemas import AnswerTrace


class TraceStore:
    def __init__(self, dir: str | Path | None = None) -> None:
        self._mem: dict[str, dict] = {}
        self.dir = Path(dir) if dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, trace: AnswerTrace) -> str:
        row = json.loads(trace.model_dump_json())
        self._mem[trace.run_id] = row
        if self.dir is not None:
            (self.dir / f"{trace.run_id}.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return trace.run_id

    def get(self, trace_id: str) -> dict | None:
        if trace_id in self._mem:
            return self._mem[trace_id]
        if self.dir is not None:
            p = self.dir / f"{trace_id}.json"
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def __len__(self) -> int:
        return len(self._mem)
