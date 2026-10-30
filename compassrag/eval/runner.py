"""评测骨架：按题跑 step_fn，增量落盘、断点续跑（按 id 跳过已完成）。"""

import json
from pathlib import Path


def run_records(records: list[dict], step_fn, out_path: Path) -> tuple[list[dict], int]:
    """对 records 逐条调 step_fn(record) -> dict，追加写入 out_path。

    out_path 已有的 id 直接跳过（断点续跑）；返回 (本轮新跑的行, 跳过数)。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    existing: list[dict] = []
    dirty = False
    if out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    dirty = True
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    dirty = True
                    continue
                if not isinstance(row, dict) or not row.get("id"):
                    dirty = True
                    continue
                row_id = str(row["id"])
                if row_id in done:
                    dirty = True
                    continue
                done.add(row_id)
                existing.append(row)

    new_rows: list[dict] = []
    skipped = sum(1 for r in records if str(r["id"]) in done)
    mode = "w" if dirty else "a"
    with out_path.open(mode, encoding="utf-8") as f:
        if dirty:
            for row in existing:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        for r in records:
            if str(r["id"]) in done:
                continue
            row = step_fn(r)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            new_rows.append(row)
    return new_rows, skipped
