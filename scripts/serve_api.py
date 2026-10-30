#!/usr/bin/env python3
"""启动 ResearchTool HTTP 服务（阶段 E）。

按 `configs/ablation/*.yaml` 的开关装载检索器，把 Agent 包成 FastAPI 应用：

    python scripts/serve_api.py --config configs/ablation/plus_full_words.yaml --port 8000
    curl -s localhost:8000/health | jq
    curl -s -X POST localhost:8000/research -H 'content-type: application/json' \
         -d '{"query":"...","budget_tokens":4000}' | jq

索引不存在时不会假装健康：`/health` 会列出每个检索器的规模与版本指纹。
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.agent import Agent, AgentConfig
from compassrag.api.server import create_app
from compassrag.index.kg import KGSearcher
from compassrag.index.wiki import WikiIndex
from compassrag.llm.client import LLMClient
from compassrag.retrieval.bm25 import BM25Index
from compassrag.retrieval.dense import DenseIndex, LocalDenseEmbedder



def _check_manifest(manifest_dir: Path, args, n_chunks: int) -> None:
    """把当前索引与最近一次 run 的 manifest 对一下：块数不一致就明确报警。

    这只是"发现不一致"，不代表数据坏了——可能是换了语料变体。写清楚原因比沉默好。
    """
    from compassrag.eval.manifest import read_manifest

    path = Path(manifest_dir) / f"{args.benchmark}__{Path(args.config).stem}.json"
    manifest = read_manifest(path)
    if manifest is None:
        print(f"[check] 没有找到 manifest：{path}（先跑一次评测再回来对账）")
        return
    flag = "OK" if manifest.corpus_chunks == n_chunks else "不一致"
    print(f"[check] manifest {path.name}：corpus_chunks={manifest.corpus_chunks} "
          f"实测={n_chunks} → {flag}")
    if manifest.corpus_chunks != n_chunks:
        print("[check] ⚠ 索引与 manifest 不对应：确认是否换了语料变体，或重建过索引。")

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=REPO_ROOT / "configs" / "ablation" / "plus_full_words.yaml")
    ap.add_argument("--benchmark", default="musique")
    ap.add_argument("--variant", default="samples")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--device", default=None, help="dense 编码设备（mps / cpu）")
    ap.add_argument("--trace-dir", default=REPO_ROOT / "runs" / "api_traces")
    ap.add_argument("--corpus-dir", default=REPO_ROOT / "data" / "cache" / "corpus")
    ap.add_argument("--index-dir", default=REPO_ROOT / "data" / "cache" / "bm25")
    ap.add_argument("--embeddings-dir", default=REPO_ROOT / "data" / "cache" / "embeddings")
    ap.add_argument("--wiki-dir", default=REPO_ROOT / "data" / "cache" / "wiki")
    ap.add_argument("--kg-dir", default=REPO_ROOT / "data" / "cache" / "kg")
    ap.add_argument("--index-source", choices=["dataset", "ingest"], default="dataset",
                    help="dataset=用 data/cache 里的基准索引；ingest=用上传文档构建的最新版本")
    ap.add_argument("--ingest-root", default=REPO_ROOT / "runs" / "ingest",
                    help="文档/索引版本目录（Task 4）")
    ap.add_argument("--runtime-index-version", default="",
                    help="服务启动时装载的索引版本标识（写进 RunRecord 与 /index/status）")
    ap.add_argument("--check", action="store_true",
                    help="只装配并打印 /health 的索引信息后退出（不监听端口、不调模型）")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    agent_cfg = AgentConfig(**cfg["agent"])
    runtime_version = args.runtime_index_version
    if args.index_source == "ingest":
        from compassrag.api.ingest import IngestStore
        ingest = IngestStore(args.ingest_root)
        latest = ingest.latest_version()
        if latest is None:
            raise SystemExit(f"没有可用的索引版本：{args.ingest_root}（先上传文档并构建）")
        tools, store = ingest.load_tools(latest.version)
        runtime_version = runtime_version or latest.version
        chunks = list(store.values())
        print(f"[ingest] 版本 {latest.version}：{len(chunks)} 块，工具 {list(tools)}（仅 BM25）")
    else:
        chunks_path = Path(args.corpus_dir) / f"{args.benchmark}__{args.variant}" / "chunks.jsonl"
        if not chunks_path.exists():
            raise SystemExit(f"语料不存在：{chunks_path}（先跑 scripts/build_corpus.py）")
        chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        store = {c["chunk_id"]: c for c in chunks}

    if args.index_source == "dataset":
        tools = {"bm25": BM25Index.load(Path(args.index_dir) / f"{args.benchmark}__{args.variant}")}
    embedder = None
    if args.index_source == "dataset" and agent_cfg.use_dense:
        embedder = LocalDenseEmbedder(device=args.device)
        tools["dense"] = DenseIndex.load(
            Path(args.embeddings_dir) / f"{args.benchmark}__{args.variant}",
            [c["chunk_id"] for c in chunks], embedder=embedder)
    if args.index_source == "dataset" and agent_cfg.use_wiki and "dense" in tools:
        tools["wiki"] = WikiIndex.load(Path(args.wiki_dir) / f"{args.benchmark}__{args.variant}",
                                       dense=tools["dense"], chunks=chunks)
    if args.index_source == "dataset" and agent_cfg.use_kg and embedder is not None:
        tools["kg"] = KGSearcher.load(Path(args.kg_dir) / f"{args.benchmark}__{args.variant}",
                                      embedder=embedder)

    agent = Agent(LLMClient(), tools, store, agent_cfg)
    app = create_app(agent, trace_dir=args.trace_dir, ingest_root=args.ingest_root,
                     runtime_index_version=runtime_version)
    if args.check:
        from fastapi.testclient import TestClient
        body = TestClient(app).get("/health").json()
        print(json.dumps(body, ensure_ascii=False, indent=2))
        _check_manifest(REPO_ROOT / "runs" / "manifests", args, len(chunks))
        return
    print(f"工具：{list(tools)}｜语料 {len(chunks)} 块｜监听 http://{args.host}:{args.port}")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
