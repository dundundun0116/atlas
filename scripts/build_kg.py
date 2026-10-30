#!/usr/bin/env python3
"""实体图构建（HippoRAG 式索引，scripts 对应 compassrag/index/kg.py）。

Phase A（--extract，LLM 并发）：逐块开放信息抽取（实体 + 三元组，JSON），
逐块增量落盘 extractions.jsonl、按 chunk_id 断点续跑；失败块不写文件，下次自动重试。
Phase B（--finalize，本地零 API）：唯一节点嵌入（BGE-M3）→ 同义边 → 图落盘
（graph.json + node_vectors.npy）。全量构建完成后先冒烟再跑全量（实验规矩）。
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from threading import Lock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.index.kg import EntityGraph, Extraction, extract_chunk
from compassrag.llm.client import LLMClient
from compassrag.retrieval.dense import LocalDenseEmbedder


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(l)["chunk_id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default="musique")
    ap.add_argument("--variant", default="samples",
                    help="语料变体：samples（抽样题小库，默认）/ full_dev（全量 dev，大库）")
    ap.add_argument("--extract", action="store_true", help="Phase A：LLM 逐块抽取")
    ap.add_argument("--finalize", action="store_true", help="Phase B：本地嵌入节点 + 同义边 + 落盘图")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="只抽取前 N 块（冒烟用）")
    ap.add_argument("--corpus-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    ap.add_argument("--kg-dir", default=REPO_ROOT / "data" / "cache" / "kg")
    ap.add_argument("--device", default=None, help="finalize 的嵌入设备（mps / cpu）")
    args = ap.parse_args()
    if not (args.extract or args.finalize):
        ap.error("至少指定 --extract 或 --finalize 之一")

    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        out_dir = Path(args.kg_dir) / f"{bench}__{args.variant}"
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks = [json.loads(l) for l in
                  (Path(args.corpus_dir) / f"{bench}__{args.variant}" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        if args.limit:
            chunks = chunks[:args.limit]
        print(f"[{bench}] 语料 {len(chunks)} 块 → {out_dir}")

        if args.extract:
            _extract(chunks, out_dir, args)
        if args.finalize:
            _finalize(chunks, out_dir, args)


def _extract(chunks, out_dir: Path, args) -> None:
    meta_path = out_dir / "meta.json"
    llm = LLMClient()
    meta = {"model": llm.model, "n_chunks": len(chunks), "stage": "extract"}
    if meta_path.exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        if old.get("model") != meta["model"]:
            raise SystemExit(f"抽取模型变更（{old.get('model')} -> {meta['model']}），结果不可混用——清理目录后重跑")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    done = _load_done(out_dir / "extractions.jsonl")
    todo = [c for c in chunks if c["chunk_id"] not in done]
    print(f"[extract] 已完成 {len(done)}，待跑 {len(todo)}，并发 {args.concurrency}")
    if not todo:
        return

    lock = Lock()
    counters = {"ok": 0, "fail": 0}
    t0 = time.perf_counter()

    def work(c):
        passage = f"{c['title']}: {c['text']}" if c.get("title") else c["text"]
        ex = extract_chunk(llm, passage)
        return c["chunk_id"], ex

    with (out_dir / "extractions.jsonl").open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(work, c) for c in todo]
            for i, fut in enumerate(as_completed(futures), start=1):
                chunk_id, ex = fut.result()
                with lock:
                    if ex is None:
                        counters["fail"] += 1
                    else:
                        ex.chunk_id = chunk_id
                        f.write(json.dumps(asdict(ex), ensure_ascii=False) + "\n")
                        counters["ok"] += 1
                    if i % 200 == 0 or i == len(todo):
                        f.flush()
                        rate = i / (time.perf_counter() - t0)
                        eta = (len(todo) - i) / rate / 60 if rate > 0 else -1
                        print(f"  进度 {i}/{len(todo)}（成功 {counters['ok']} 失败 {counters['fail']}），"
                              f"{rate:.1f} 块/秒，剩余约 {eta:.0f} 分钟")
    print(f"[extract] 完成：成功 {counters['ok']} / 失败 {counters['fail']}（失败块可重跑补齐）")


def _finalize(chunks, out_dir: Path, args) -> None:
    ext_path = out_dir / "extractions.jsonl"
    valid_ids = {c["chunk_id"] for c in chunks}
    rows = [json.loads(l) for l in ext_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    outside = [r for r in rows if r["chunk_id"] not in valid_ids]
    if outside:
        # 抽取文件超出语料（如变体错位）：只建语料内的块，防止图与评测语料不一致
        print(f"[finalize] 警告：抽取文件含 {len(outside)} 条语料外记录，已过滤"
              f"（{len(rows) - len(outside)} 条入库）")
        rows = [r for r in rows if r["chunk_id"] in valid_ids]
    done = {r["chunk_id"] for r in rows}
    missing = len(chunks) - len(done)
    if missing > 0:
        print(f"[finalize] 警告：还有 {missing} 块未抽取（将按现有 {len(done)} 块建图；"
              f"补齐后可重新 finalize 覆盖）")
    graph = EntityGraph()
    for row in rows:
        graph.add_extraction(Extraction(chunk_id=row["chunk_id"],
                                        entities=row["entities"], triples=row["triples"]))
    print(f"[finalize] {len(graph.nodes)} 节点 / {len(graph._edges)} 三元组边 / {len(graph.passages)} 块")
    embedder = LocalDenseEmbedder(device=args.device, batch_size=128)  # 唯一节点可达十万级
    graph.finalize(embedder, log=print)
    graph.save(out_dir)
    print(f"[finalize] 图已落盘：{out_dir / 'graph.json'} + node_vectors.npy")


if __name__ == "__main__":
    main()
