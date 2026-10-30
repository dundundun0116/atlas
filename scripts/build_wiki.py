#!/usr/bin/env python3
"""构建 wiki 式分层摘要索引（③ 的地基）：聚类 → LLM 生成条目 → 条目向量 → 落盘。

产物 data/cache/wiki/{bench}__full_dev/：labels.npy（簇标签缓存）、entries.jsonl
（逐簇增量、断点续跑）、entry_vectors.npy（条目层检索向量）、meta.json（参数守卫）。
LLM 用量 ≈ 簇数 k（默认 = 块数/100）；失败簇跳过并列出，重跑可补。
冒烟：--limit 3 只生成前 3 个簇。

建议后台跑：nohup .venv/bin/python scripts/build_wiki.py --benchmarks musique > runs/wiki_build.log 2>&1 &
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.index.cluster import CHUNKS_PER_ENTRY_DEFAULT, KMEANS_SEED_DEFAULT, default_k
from compassrag.index.wiki import build_entry_vectors, build_wiki, load_entries
from compassrag.llm.client import LLMClient
from compassrag.retrieval.dense import LocalDenseEmbedder, load_embeddings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default="musique")
    ap.add_argument("--corpus-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    ap.add_argument("--embeddings-dir", default=REPO_ROOT / "data" / "cache" / "embeddings")
    ap.add_argument("--out-dir", default=REPO_ROOT / "data" / "cache" / "wiki")
    ap.add_argument("--k", type=int, default=None, help="簇数（默认 = 块数/100）")
    ap.add_argument("--chunks-per-entry", type=int, default=CHUNKS_PER_ENTRY_DEFAULT)
    ap.add_argument("--seed", type=int, default=KMEANS_SEED_DEFAULT)
    ap.add_argument("--limit", type=int, default=None, help="只生成前 N 个簇（冒烟用）")
    ap.add_argument("--device", default=None, help="条目向量编码设备（mps / cpu）")
    args = ap.parse_args()

    llm = LLMClient()
    embedder = LocalDenseEmbedder(device=args.device)

    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        chunks_path = Path(args.corpus_dir) / f"{bench}__full_dev" / "chunks.jsonl"
        chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        emb_dir = Path(args.embeddings_dir) / f"{bench}__full_dev"
        vectors = load_embeddings(emb_dir, len(chunks))
        out_dir = Path(args.out_dir) / f"{bench}__full_dev"
        k = args.k or default_k(len(chunks))

        print(f"[{bench}] {len(chunks)} 块 → k={k} 簇；本次最多生成 {min(k, args.limit) if args.limit else k} 个条目"
              f"（LLM 调用数 ≈ 此值）", flush=True)
        t0 = time.time()
        stats = build_wiki(chunks, vectors, out_dir, llm, k=k,
                           chunks_per_entry=args.chunks_per_entry, seed=args.seed, limit=args.limit)
        entries = load_entries(out_dir / "entries.jsonl")
        vecs = build_entry_vectors(entries, embedder)
        np.save(out_dir / "entry_vectors.npy", vecs)
        print(f"[{bench}] 新建 {stats['built']} / 跳过已存在 {stats['skipped']} / 失败 {stats['failed']}；"
              f"条目总数 {len(entries)}，条目向量 {vecs.shape}；耗时 {time.time() - t0:.0f}s", flush=True)
        if stats["failed_ids"]:
            print(f"[{bench}] 失败簇（重跑可补）：{stats['failed_ids']}", flush=True)


if __name__ == "__main__":
    main()
