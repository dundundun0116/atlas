#!/usr/bin/env python3
"""三基准就绪度检查（M3）：缺什么、要花什么代价补。

规则：**能用小库就不用全量**。2wiki/hotpotqa 目前只有 full_dev 语料
（58510 / 66946 块），没有 samples 小库、没有查询 NER 缓存、没有实体图；
实体图抽取是付费 LLM 步骤，跑之前必须先报预算。
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ("musique", "2wiki", "hotpotqa")


def _chunks(cache: Path, bench: str, variant: str) -> int:
    p = cache / "corpus" / f"{bench}__{variant}" / "chunks.jsonl"
    if not p.exists():
        return 0
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def _qa_runs(runs: Path, bench: str) -> int:
    files = [p for p in (runs / "qa").glob(f"{bench}__*.jsonl") if not p.name.startswith("_invalid_")]
    return sum(1 for p in files for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def _traces(runs: Path, bench: str) -> int:
    files = list((runs / "traces").glob(f"{bench}__*.jsonl"))
    return sum(1 for p in files for line in p.read_text(encoding="utf-8").splitlines() if line.strip())


def checks(bench: str, cache: Path, data: Path, runs: Path) -> list[tuple[str, bool, str]]:
    samples = sorted((data / "samples").glob(f"{bench}_dev_n*_seed*.jsonl"))
    kg = cache / "kg" / f"{bench}__samples"
    n_qa, n_tr = _qa_runs(runs, bench), _traces(runs, bench)
    return [
        ("抽样清单", bool(samples), samples[-1].name if samples else "缺 data/samples/*"),
        ("samples 语料", bool(_chunks(cache, bench, "samples")),
         f"{_chunks(cache, bench, 'samples')} 块"),
        ("BM25(samples)", (cache / "bm25" / f"{bench}__samples").exists(), "需 build 索引"),
        ("稠密向量(samples)", (cache / "embeddings" / f"{bench}__samples").exists(), "需 embed_corpus"),
        ("实体图(samples)", (kg / "graph.json").exists(), "付费：实体抽取"),
        ("查询 NER 缓存", (kg / "query_entities.jsonl").exists(), "付费：查询实体识别"),
        ("QA 结果", n_qa > 0, f"{n_qa} 题"),
        ("轨迹", n_tr > 0, f"{n_tr} 题"),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=REPO_ROOT / "data" / "cache")
    ap.add_argument("--data", default=REPO_ROOT / "data")
    ap.add_argument("--runs", default=REPO_ROOT / "runs")
    args = ap.parse_args()
    cache, data, runs = Path(args.cache), Path(args.data), Path(args.runs)

    print(f"{'检查项':<20}" + "".join(f"{b:>12}" for b in BENCHMARKS))
    print("-" * (20 + 12 * len(BENCHMARKS)))
    rows = {b: checks(b, cache, data, runs) for b in BENCHMARKS}
    for i, (name, _, _) in enumerate(rows["musique"]):
        cells = []
        for b in BENCHMARKS:
            ok, detail = rows[b][i][1], rows[b][i][2]
            cells.append(f"{'✅' if ok else '❌':>3}{detail[:8]:>9}")
        print(f"{name:<20}" + "".join(cells))

    print("\n缺口（按基准）：")
    for b in BENCHMARKS:
        missing = [name for name, ok, _ in rows[b] if not ok]
        full = _chunks(cache, b, "full_dev")
        print(f"- {b}: " + ("齐了" if not missing else "、".join(missing))
              + (f"（full_dev {full} 块可作退路，但评测成本高）" if full and _chunks(cache, b, 'samples') == 0 else ""))
    print("\n注：实体图与查询 NER 是付费 LLM 步骤；跑之前先报预算等批准（见 upgrades 计划 §9.4）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
