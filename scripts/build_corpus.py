#!/usr/bin/env python3
"""构建 distractor 共享检索语料库（默认全量 dev context 池）。

输出目录按模式编码：data/cache/corpus/{bench}__{mode}/，互不覆盖。
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.corpus.benchmarks import BENCHMARKS
from compassrag.corpus.build import build_corpus


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default=",".join(BENCHMARKS))
    ap.add_argument("--from-samples", action="store_true",
                    help="只用抽样题的 context 池建库（小库模式）")
    ap.add_argument("--raw-dir", default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--samples-dir", default=REPO_ROOT / "data" / "samples")
    ap.add_argument("--out-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    args = ap.parse_args()

    stats_list = []
    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        stats_list.append(build_corpus(bench, args.raw_dir, args.out_dir,
                                       from_samples=args.from_samples,
                                       samples_dir=args.samples_dir))

    print(f"{'基准':<10} {'模式':<10} {'题数':>6} {'唯一段落':>8} {'块数':>7} {'≈token':>10} "
          f"{'块长min/中位/p95/max':>22}")
    for s in stats_list:
        print(f"{s['benchmark']:<10} {s['mode']:<10} {s['n_questions']:>6} "
              f"{s['n_unique_paragraphs']:>8} {s['n_chunks']:>7} {s['n_tokens_est_total']:>10} "
              f"{s['char_len_min']}/{s['char_len_median']}/{s['char_len_p95']}/{s['char_len_max']:>18}")


if __name__ == "__main__":
    main()
