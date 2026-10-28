#!/usr/bin/env python3
"""把 runs/qa 读成一张四列表：质量 / 证据 / 成本 / 可靠性（M3）。

默认跳过 `_invalid_` 前缀的作废口径文件；加 `--stratify` 按 stratum 展开
（**同一基准内部**才可比，见 lessons/E1）。

用法：
  python scripts/report_runs.py
  python scripts/report_runs.py --benchmarks musique --stratify
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.eval.report import (  # noqa: E402
    comparability_warnings,
    format_table,
    load_runs,
    report_run,
    summarize,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qa-dir", default=REPO_ROOT / "runs" / "qa")
    ap.add_argument("--benchmarks", default="", help="逗号分隔；空 = 全部")
    ap.add_argument("--stratify", action="store_true", help="按 stratum 展开（同基准内）")
    ap.add_argument("--include-invalid", action="store_true", help="连作废口径一起读（不建议）")
    args = ap.parse_args()

    benches = [b.strip() for b in args.benchmarks.split(",") if b.strip()] or None
    runs = load_runs(args.qa_dir, include_invalid=args.include_invalid, benchmarks=benches)
    if not runs:
        print(f"没有可用结果：{args.qa_dir}")
        return 1

    print(format_table({name: summarize(rows) for name, rows in runs.items()}))
    for warning in comparability_warnings(runs):
        print(f"⚠ {warning}")
    for name, rows in runs.items():
        missing = sorted({"em", "f1"} - set(rows[0]))
        if missing:
            print(f"\n⚠ {name} 缺字段 {missing}（旧格式行，建议用 eval/traces.py 回放重算）")
    if args.stratify:
        for name, rows in runs.items():
            print("\n" + report_run(name, rows, stratify=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
