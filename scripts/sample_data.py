#!/usr/bin/env python3
"""三基准分层抽样：data/samples/{bench}_dev_n{N}_seed{S}.jsonl + manifest。

抽样清单只存 {benchmark, id, stratum}，题面与证据按 id 从原始 dev 关联。
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.corpus.benchmarks import BENCHMARKS, load_dev, source_sha256
from compassrag.corpus.sample import stratified_sample, write_samples


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default=",".join(BENCHMARKS))
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--raw-dir", default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--out-dir", default=REPO_ROOT / "data" / "samples")
    args = ap.parse_args()

    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        records = load_dev(bench, Path(args.raw_dir))
        picked, alloc = stratified_sample(records, args.n, seed=args.seed)
        out = Path(args.out_dir) / f"{bench}_dev_n{args.n}_seed{args.seed}.jsonl"
        source = _source(bench, args.raw_dir)
        manifest = write_samples(
            bench, picked, alloc, seed=args.seed, out_path=out,
            n_population=len(records), source_path=source)
        print(f"[{bench}] 总体 {len(records)} -> 抽样 {len(picked)}，分配 {alloc}")
        print(f"        {out.name}  sha256={manifest['output_sha256'][:16]}…")


def _source(bench: str, raw_dir) -> Path:
    files = sorted(p for p in (Path(raw_dir) / bench).glob("*") if p.suffix != ".part")
    if not files:
        raise FileNotFoundError(f"{bench} 的原始文件不存在，先运行 download_dev")
    return files[0]


if __name__ == "__main__":
    main()
