#!/usr/bin/env python3
"""双臂对照：按预注册判据判定答案格式实验（H1/H2/partial）。

用法：
  python scripts/compare_arms.py --base plus_full --arm plus_full_words
判据定义见 `docs/experiment-answer-format.md` 与 `compassrag/eval/arms.py`。
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.eval.arms import arm_summary, judge_format_arm  # noqa: E402
from compassrag.eval.report import load_rows  # noqa: E402


def _rows(qa_dir: Path, bench: str, name: str, variant: str) -> list[dict]:
    suffix = "" if variant == "full_dev" else f"__{variant}"
    path = qa_dir / f"{bench}__{name}{suffix}.jsonl"
    if not path.exists():
        raise SystemExit(f"缺少结果文件：{path}")
    return load_rows(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="plus_full")
    ap.add_argument("--arm", default="plus_full_words")
    ap.add_argument("--bench", default="musique")
    ap.add_argument("--variant", default="samples")
    ap.add_argument("--qa-dir", default=REPO_ROOT / "runs" / "qa")
    args = ap.parse_args()

    base = arm_summary(_rows(Path(args.qa_dir), args.bench, args.base, args.variant))
    arm = arm_summary(_rows(Path(args.qa_dir), args.bench, args.arm, args.variant))
    verdict = judge_format_arm(base, arm)
    print(json.dumps({"base": {args.base: base}, "arm": {args.arm: arm},
                      "verdict": verdict}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
