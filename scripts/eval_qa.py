#!/usr/bin/env python3
"""端到端 QA 评测（消融第 0 级 naive）：scripted agent + 直接生成 → EM/F1 + 过程指标。

两层落盘（M0 起）：
- `runs/traces/{bench}__{config}.jsonl`：逐题完整轨迹（查询来源 / 工具调用 / 证据 /
  停止原因），可离线回放重算指标；
- `runs/qa/{bench}__{config}.jsonl`：由轨迹派生的指标行，供聚合与表格。

逐题增量落盘 + 断点续跑；注意需要可用的 LLM 通道（402/403 会即刻失败，属预期）。
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
from compassrag.eval.manifest import build_manifest, manifest_path, write_manifest
from compassrag.eval.runner import run_records
from compassrag.eval.traces import derive_row, trace_path, write_traces
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
    ap.add_argument("--trace-dir", default=REPO_ROOT / "runs" / "traces",
                    help="逐题轨迹落盘目录（M0：可回放）")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑分层比例子集的前 N 题（能子集不跑全量；后补全量时按 id 续跑天然复用）")
    ap.add_argument("--device", default=None, help="dense 查询编码设备（mps / cpu；后台向量化时用 cpu 避免抢 MPS）")
    ap.add_argument("--manifest-dir", default=REPO_ROOT / "runs" / "manifests",
                    help="运行身份快照目录（配置/模型/语料/种子/git）")
    ap.add_argument("--max-total-tokens", type=int, default=0,
                    help="本题 token 上限（0=不限）；同预算对照实验用它")
    ap.add_argument("--max-latency-ms", type=float, default=0.0,
                    help="本题墙钟上限（0=不限）")
    ap.add_argument("--no-graph", dest="use_graph", action="store_false",
                    help="退回顺序实现（默认走 LangGraph runner；复现历史行时用）")
    ap.set_defaults(use_graph=True)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    agent_cfg = AgentConfig(**cfg["agent"])
    if args.max_total_tokens:
        agent_cfg.max_total_tokens = args.max_total_tokens
    if args.max_latency_ms:
        agent_cfg.max_latency_ms = args.max_latency_ms
    agent_cfg.use_graph = args.use_graph
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
        trace_file = trace_path(args.trace_dir, bench, config_name)
        seed = _seed_of(sample_file)
        manifest = build_manifest(
            repo=REPO_ROOT, benchmark=bench, config_name=config_name,
            config=agent_cfg.snapshot(), model=getattr(llm, "model", ""),
            embedding_model=getattr(llm, "embedding_model", ""),
            corpus_variant=args.variant, chunk_ids=[c["chunk_id"] for c in chunks],
            sample_file=sample_file.name, sample_ids=[r["id"] for r in sampled], seed=seed)
        write_manifest(manifest, manifest_path(args.manifest_dir, bench, config_name))
        fp = manifest.fingerprint()
        print(f"[{bench}] manifest {fp}（模型 {manifest.model or '?'}｜语料 {manifest.corpus_hash}）")

        def step_fn(q, agent=agent, trace_file=trace_file, fp=fp):
            r = agent.answer(q["question"])
            trace = r.trace
            trace.config["question_id"] = q["id"]
            trace.config["benchmark"] = q.get("benchmark") or bench
            trace.config["manifest_fp"] = fp
            write_traces([trace], trace_file, append=True)
            gold_titles = {p["title"] for p in q["paragraphs"] if p["is_supporting"]}
            row = derive_row(trace, gold=q["answer"], gold_titles=gold_titles,
                             question_id=q["id"], stratum=q["stratum"],
                             k=agent_cfg.top_k)
            row["manifest_fp"] = fp
            return row

        suffix = "" if args.variant == "full_dev" else f"__{args.variant}"
        out = Path(args.out_dir) / f"{bench}__{config_name}{suffix}.jsonl"
        rows, skipped = run_records(sampled, step_fn, out)
        all_rows = _load_all(out)
        summary[bench] = _aggregate(all_rows)
        print(f"[{bench}] 完成 {len(rows)} 题（续跑跳过 {skipped}）→ {out}"
              f"；轨迹 {trace_file}")

    print(f"\n{'基准':<10} {'题数':>5} {'EM':>7} {'F1':>7} {'召回':>7} {'full-hit':>9} "
          f"{'空答':>7} {'轮数':>6} {'早停':>7} {'均token':>9}")
    for bench, s in summary.items():
        print(f"{bench:<10} {s['n']:>5} {s['em']:>7.3f} {s['f1']:>7.3f} {s['ev_recall']:>7.3f} "
              f"{s['ev_full_hit']:>9.3f} {s['empty_rate']:>7.3f} {s['rounds_per_q']:>6.2f} "
              f"{s['early_stop_rate']:>7.3f} {s['tokens_per_q']:>9.0f}")
    if summary:
        stops = {}
        for s in summary.values():
            for k, v in s["stop_reasons"].items():
                stops[k] = stops.get(k, 0) + v
        print(f"停止原因分布：{stops}")



def _seed_of(sample_file):
    """从 `xxx_dev_n300_seed42.jsonl` 里取种子；取不到返回 None。"""
    stem = sample_file.stem
    for part in stem.split("_"):
        if part.startswith("seed"):
            try:
                return int(part[4:])
            except ValueError:
                return None
    return None


def _load_all(out: Path) -> list[dict]:
    if not out.exists():
        return []
    return [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]


def _aggregate(rows: list[dict]) -> dict:
    n = len(rows) or 1
    stops: dict[str, int] = {}
    for r in rows:
        stops[r.get("stop_reason", "")] = stops.get(r.get("stop_reason", ""), 0) + 1
    return {
        "n": len(rows),
        "em": sum(r["em"] for r in rows) / n,
        "f1": sum(r["f1"] for r in rows) / n,
        "ev_recall": sum(r["ev_recall"] for r in rows) / n,
        "ev_full_hit": sum(r.get("ev_full_hit", 0.0) for r in rows) / n,
        "tokens_per_q": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows) / n,
        "rounds_per_q": sum(r.get("rounds", 0) for r in rows) / n,
        "early_stop_rate": sum(1 for r in rows if r.get("stop_reason") == "early_stop") / n,
        # 空答率应趋近 0；非 0 说明生成协议仍被思维链打转拖垮（见 runs/diag/）
        "empty_rate": sum(1 for r in rows if not (r["pred"] or "").strip()) / n,
        "retry_rate": sum(1 for r in rows if r.get("n_calls", 1) > 1) / n,
        "tool_failure_rate": sum(1 for r in rows if r.get("n_tool_failures")) / n,
        "parse_fallback_rate": sum(1 for r in rows if r.get("parse_fallbacks")) / n,
        "stop_reasons": dict(sorted(stops.items())),
    }


if __name__ == "__main__":
    main()

