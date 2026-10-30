#!/usr/bin/env python3
"""端到端任务级评测入口（离线脚手架）。

    python scripts/run_merged_e2e.py                 # 跑全部场景，写 runs/merged_e2e/summary.json
    python scripts/run_merged_e2e.py --out runs/xxx  # 指定产物目录

度量的是「Atlas 编排 × CompassRAG 检索 × 暂停/恢复」这条链路是否闭合、
失败是否可分类、证据是否可追溯；模型与检索器都是脚本化替身，**不是效果数字**。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integration.merged_e2e import format_table, run_all  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "runs" / "merged_e2e"),
                        help="产物目录（默认 runs/merged_e2e）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    args = parser.parse_args()

    summary = run_all(args.out)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(format_table(summary))
    print(f"\n产物：{os.path.join(args.out, 'summary.json')}")


if __name__ == "__main__":
    main()
