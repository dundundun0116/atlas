"""把各臂结果汇总成 Markdown 表（README 成绩表的数据源）。

用法：external/MemoryArena/.venv/bin/python eval/summarize.py [结果目录] [--out 表格.md]
- 默认结果目录 runs/json/math；逐臂（子目录）复用官方 eval.py 的口径计算
  progress score / pass rate / 平均上下文 tokens，并在臂目录写官方 all_results.json；
- 输出 Markdown 表（可直接贴进 README），同时打印到终端。

口径说明（官方 eval.py）：progress score = 子任务正确率均值；pass rate = 末题正确的任务占比；
memory_length = 每步 memory_context 的 token 数均值（省 token 卖点的度量）。
"""

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "external", "MemoryArena"))

import numpy as np  # noqa: E402
from env.env_systems.formal_reasoning_env.eval import (  # noqa: E402
    get_passrate_at_k,
    print_result,
    process_all_papers_for_method,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")


def summarize(base_dir: str, out_path: str = None) -> str:
    logger = logging.getLogger("summarize")
    rows = []
    for arm in sorted(os.listdir(base_dir)):
        arm_dir = os.path.join(base_dir, arm)
        if not os.path.isdir(arm_dir):
            continue
        data, _ = process_all_papers_for_method(arm_dir, logger=logger)
        if not data:
            continue
        paper_passrate, prate_at_k, count_at_k, min_length = get_passrate_at_k(data, logger=logger)
        print_result(paper_passrate, prate_at_k, count_at_k, min_length, arm_dir, logger=logger)
        rows.append(
            (
                arm,
                float(np.mean([v["progress_score"] for v in paper_passrate.values()])),
                float(np.mean([v["is_paper_correct"] for v in paper_passrate.values()])),
                float(np.mean([v["avg_length"] for v in paper_passrate.values()])),
                len(paper_passrate),
            )
        )

    lines = [
        "| 臂 | progress score | pass rate | 平均上下文 tokens | 任务数 |",
        "|---|---|---|---|---|",
    ]
    for arm, ps, pr, mem, n in rows:
        lines.append(f"| {arm} | {ps:.3f} | {pr:.3f} | {mem:.0f} | {n} |")
    table = "\n".join(lines)
    print("\n" + table)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(table + "\n")
        print(f"\n已写入：{out_path}")
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总各臂评测结果（Markdown 表）")
    parser.add_argument("results_dir", nargs="?", default=os.path.join(ROOT, "runs", "json", "math"))
    parser.add_argument("--out", default=None, help="Markdown 输出路径")
    args = parser.parse_args()
    summarize(args.results_dir, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
