#!/usr/bin/env python3
"""查询侧实体识别缓存（实体图检索的种子来源）：每题一次 LLM NER，结果落盘复用。

产出 data/cache/kg/{bench}__full_dev/query_entities.jsonl（逐题、断点续跑）。
关思维链（机械任务）；未解析出的题不写文件，重跑自动补。ner 失败的题在检索时
自动回退"整查询嵌入 top-k 节点"种子，不阻塞评测。
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.corpus.benchmarks import BENCHMARKS, load_dev
from compassrag.index.kg import ner_query
from compassrag.llm.client import LLMClient


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default=",".join(BENCHMARKS))
    ap.add_argument("--variant", default="samples",
                    help="语料变体：samples（抽样题小库，默认）/ full_dev")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--raw-dir", default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--samples-dir", default=REPO_ROOT / "data" / "samples")
    ap.add_argument("--kg-dir", default=REPO_ROOT / "data" / "cache" / "kg")
    args = ap.parse_args()

    llm = LLMClient()
    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        out_dir = Path(args.kg_dir) / f"{bench}__{args.variant}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / "query_entities.jsonl"

        records = load_dev(bench, Path(args.raw_dir))
        sample_file = sorted(Path(args.samples_dir).glob(f"{bench}_dev_n*_seed*.jsonl"))[-1]
        sample_ids = {json.loads(l)["id"] for l in sample_file.read_text().splitlines() if l.strip()}
        sampled = [r for r in records if r["id"] in sample_ids]
        if args.limit:
            sampled = sampled[:args.limit]

        done: set[str] = set()
        if out.exists():
            done = {json.loads(l)["id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}
        todo = [r for r in sampled if r["id"] not in done]
        print(f"[{bench}] 待 NER {len(todo)} 题（已完成 {len(done)}），并发 {args.concurrency}")
        if not todo:
            continue

        lock = Lock()
        counters = {"ok": 0, "empty": 0, "fail": 0}

        def work(q):
            return q["id"], q["question"], ner_query(llm, q["question"])

        with out.open("a", encoding="utf-8") as f:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                for qid, question, entities in pool.map(work, todo):
                    with lock:
                        if entities is None:
                            counters["fail"] += 1
                            continue
                        # question 一并落盘：检索评测按问题文本取种子实体（题 id 在评测器里不可见）
                        f.write(json.dumps({"id": qid, "question": question, "entities": entities},
                                           ensure_ascii=False) + "\n")
                        counters["ok"] += 1
                        if not entities:
                            counters["empty"] += 1
        f_n = sum(counters.values())
        print(f"[{bench}] 完成：成功 {counters['ok']}（其中无实体 {counters['empty']}）/ 失败 {counters['fail']}")


if __name__ == "__main__":
    main()
