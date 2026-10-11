#!/usr/bin/env python3
"""端到端 QA 评测（消融第 0 级 naive）：scripted agent + 直接生成 → EM/F1。

逐题增量落盘 + 断点续跑；产出 runs/qa/{bench}__{config}.jsonl + 汇总表。
注意：需要可用的 LLM 通道（402/403 会即刻失败，属预期）。
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.agent import Agent, AgentConfig
from compassrag.corpus.benchmarks import BENCHMARKS, load_dev
from compassrag.corpus.sample import stratified_subset
from compassrag.eval.metrics import f1_score, em_score, recall_at_k
from compassrag.eval.runner import run_records
from compassrag.index.kg import KGSearcher
from compassrag.index.wiki import WikiIndex
from compassrag.llm.client import LLMClient
from compassrag.retrieval.bm25 import BM25Index
from compassrag.retrieval.dense import DenseIndex, LocalDenseEmbedder


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmarks", default=",".join(BENCHMARKS))
    ap.add_argument("--variant", default="full_dev", help="语料变体：full_dev（默认）/ samples")
    ap.add_argument("--config", default=REPO_ROOT / "configs" / "ablation" / "naive.yaml")
    ap.add_argument("--raw-dir", default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--samples-dir", default=REPO_ROOT / "data" / "samples")
    ap.add_argument("--corpus-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    ap.add_argument("--index-dir", default=REPO_ROOT / "data" / "cache" / "bm25")
    ap.add_argument("--embeddings-dir", default=REPO_ROOT / "data" / "cache" / "embeddings")
    ap.add_argument("--wiki-dir", default=REPO_ROOT / "data" / "cache" / "wiki")
    ap.add_argument("--kg-dir", default=REPO_ROOT / "data" / "cache" / "kg")
    ap.add_argument("--out-dir", default=REPO_ROOT / "runs" / "qa")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑分层比例子集的前 N 题（能子集不跑全量；后补全量时按 id 续跑天然复用）")
    ap.add_argument("--device", default=None, help="dense 查询编码设备（mps / cpu；后台向量化时用 cpu 避免抢 MPS）")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    agent_cfg = AgentConfig(**cfg["agent"])
    if agent_cfg.use_wiki and not agent_cfg.use_dense:
        raise SystemExit("use_wiki 依赖稠密向量通路（条目层/源块排序）——请同时开启 use_dense")
    if agent_cfg.use_kg and not agent_cfg.use_dense:
        raise SystemExit("use_kg 依赖稠密向量通路（查询/实体嵌入选种）——请同时开启 use_dense")
    llm = LLMClient()
    config_name = Path(args.config).stem
    embedder = LocalDenseEmbedder(device=args.device) if agent_cfg.use_dense else None

    summary = {}
    for bench in [b.strip() for b in args.benchmarks.split(",")]:
        records = load_dev(bench, Path(args.raw_dir))
        sample_file = sorted(Path(args.samples_dir).glob(f"{bench}_dev_n*_seed*.jsonl"))[-1]
        sample_ids = {json.loads(l)["id"] for l in sample_file.read_text().splitlines() if l.strip()}
        sampled = [r for r in records if r["id"] in sample_ids]
        if args.limit:
            sampled = stratified_subset(sampled, args.limit)
            print(f"[{bench}] --limit {args.limit}：分层比例子集 {len(sampled)} 题")

        chunks = [json.loads(l) for l in
                  (Path(args.corpus_dir) / f"{bench}__{args.variant}" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
                  if l.strip()]
        store = {c["chunk_id"]: c for c in chunks}
        tools = {"bm25": BM25Index.load(Path(args.index_dir) / f"{bench}__{args.variant}")}
        if agent_cfg.use_dense:
            tools["dense"] = DenseIndex.load(
                Path(args.embeddings_dir) / f"{bench}__{args.variant}",
                [c["chunk_id"] for c in chunks], embedder=embedder)
        if agent_cfg.use_wiki:
            tools["wiki"] = WikiIndex.load(Path(args.wiki_dir) / f"{bench}__{args.variant}",
                                           dense=tools["dense"], chunks=chunks)
        if agent_cfg.use_kg:
            tools["kg"] = KGSearcher.load(Path(args.kg_dir) / f"{bench}__{args.variant}",
                                          embedder=embedder)
        print(f"[{bench}] 语料 {len(chunks)} 块；工具：{list(tools)}")

        agent = Agent(llm=llm, tools=tools, chunk_store=store, config=agent_cfg)

        def step_fn(q, agent=agent):
            r = agent.answer(q["question"])
            gold_titles = {p["title"] for p in q["paragraphs"] if p["is_supporting"]}
            ev_titles = [store[cid]["title"] for cid in r.evidence if cid in store]
            return {
                "id": q["id"], "stratum": q["stratum"],
                "pred": r.answer, "gold": q["answer"],
                "em": em_score(r.answer, q["answer"]), "f1": f1_score(r.answer, q["answer"]),
                "ev_recall": recall_at_k(ev_titles, gold_titles, k=agent_cfg.top_k),
                "rounds": r.rounds,
                "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                # 生成协议诊断字段：重试次数与各次 finish_reason（"length"=撞预算上限）
                "n_calls": r.n_calls, "finish_reasons": r.finish_reasons,
                "answer_empty": not r.answer.strip(),
                "n_queries": len(r.queries) or 1,   # ② 开启时 >1（分解 + HyDE 变体数）
                # ① 决策层诊断字段：路由、全部 LLM 调用数（含 plan/sufficiency）
                "route": r.route, "n_llm_calls": r.n_llm_calls,
            }

        suffix = "" if args.variant == "full_dev" else f"__{args.variant}"
        out = Path(args.out_dir) / f"{bench}__{config_name}{suffix}.jsonl"
        rows, skipped = run_records(sampled, step_fn, out)
        all_rows = _load_all(out)
        summary[bench] = _aggregate(all_rows)
        print(f"[{bench}] 完成 {len(rows)} 题（续跑跳过 {skipped}）→ {out}")

    print(f"\n{'基准':<10} {'题数':>5} {'EM':>7} {'F1':>7} {'证据召回':>8} {'空答率':>7} {'重试率':>7} {'均token/题':>10}")
    for bench, s in summary.items():
        print(f"{bench:<10} {s['n']:>5} {s['em']:>7.3f} {s['f1']:>7.3f} {s['ev_recall']:>8.3f} "
              f"{s['empty_rate']:>7.3f} {s['retry_rate']:>7.3f} {s['tokens_per_q']:>10.0f}")


def _load_all(out: Path) -> list[dict]:
    if not out.exists():
        return []
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]


def _aggregate(rows: list[dict]) -> dict:
    n = len(rows) or 1
    return {
        "n": len(rows),
        "em": sum(r["em"] for r in rows) / n,
        "f1": sum(r["f1"] for r in rows) / n,
        "ev_recall": sum(r["ev_recall"] for r in rows) / n,
        "tokens_per_q": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows) / n,
        # 空答率应趋近 0；非 0 说明生成协议仍被思维链打转拖垮（见 runs/diag/）
        "empty_rate": sum(1 for r in rows if not r["pred"].strip()) / n,
        "retry_rate": sum(1 for r in rows if r.get("n_calls", 1) > 1) / n,
    }


if __name__ == "__main__":
    main()
