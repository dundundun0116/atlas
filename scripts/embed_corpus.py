#!/usr/bin/env python3
"""本地 BGE-M3 批量向量化语料（离线、断点续跑）。

输出 data/cache/embeddings/{bench}__full_dev/shard_XXX.npy + meta.json。
建议 nohup 后台跑：nohup .venv/bin/python scripts/embed_corpus.py > runs/embed.log 2>&1 &
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.corpus.benchmarks import BENCHMARKS
from compassrag.retrieval.dense import (
    MODEL_NAME_DEFAULT, SHARD_SIZE_DEFAULT, LocalDenseEmbedder, embed_shards, plan_shards,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default=",".join(BENCHMARKS))
    ap.add_argument("--variant", default="full_dev", help="语料变体：full_dev / samples")
    ap.add_argument("--corpus-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    ap.add_argument("--out-dir", default=REPO_ROOT / "data" / "cache" / "embeddings")
    ap.add_argument("--model", default=MODEL_NAME_DEFAULT)
    ap.add_argument("--device", default=None, help="mps / cpu，缺省自动")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--shard-size", type=int, default=SHARD_SIZE_DEFAULT)
    args = ap.parse_args()

    embedder = LocalDenseEmbedder(args.model, device=args.device, batch_size=args.batch_size)

    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        chunks_path = Path(args.corpus_dir) / f"{bench}__{args.variant}" / "chunks.jsonl"
        chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        out_dir = Path(args.out_dir) / f"{bench}__{args.variant}"
        total_shards = len(plan_shards(len(chunks), args.shard_size))

        t0 = time.time()
        done = embed_shards(chunks, out_dir, embedder, shard_size=args.shard_size,
                            log=lambda msg, t0=t0, total=total_shards, bench=bench:
                            print(f"[{bench}] {msg} | 已用 {time.time()-t0:.0f}s", flush=True))
        elapsed = time.time() - t0
        (out_dir / "meta.json").write_text(json.dumps({
            "benchmark": bench, "model": args.model, "n_chunks": len(chunks),
            "shard_size": args.shard_size, "n_shards": total_shards,
            "device": args.device or "auto",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[{bench}] 完成：{len(chunks)} 块 / {total_shards} 片，本次新算 {done} 片，耗时 {elapsed:.0f}s", flush=True)


if __name__ == "__main__":
    main()
