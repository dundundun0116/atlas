"""语料库构建：dev 集段落合并去重 → chunks.jsonl + stats.json。

输出目录按模式编码（{bench}__full_dev / {bench}__samples），互不覆盖。
默认用全量 dev 的 context 池（检索池更大、更接近真实设定）；
--from-samples 时只用抽样题的 context 池（更小更省，复现实验用）。
"""

import json
import statistics
from pathlib import Path

from .benchmarks import load_dev
from .chunking import iter_chunks, normalize_ws


def build_corpus_from_records(bench: str, records: list[dict], out_dir: Path, *,
                              mode: str = "full_dev",
                              max_chars: int = 1800, overlap: int = 200) -> dict:
    unique_paras = {(p["title"], normalize_ws(p["text"])) for r in records for p in r["paragraphs"]}
    chunks = list(iter_chunks(records, bench, max_chars=max_chars, overlap=overlap))

    out_dir = Path(out_dir) / f"{bench}__{mode}"
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = out_dir / "chunks.jsonl"
    with open(chunks_path, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    char_lens = sorted(c["n_chars"] for c in chunks)
    stats = {
        "benchmark": bench,
        "mode": mode,
        "n_questions": len(records),
        "n_unique_paragraphs": len(unique_paras),
        "n_chunks": len(chunks),
        "n_tokens_est_total": sum(c["n_tokens_est"] for c in chunks),
        "char_len_min": char_lens[0],
        "char_len_median": int(statistics.median(char_lens)),
        "char_len_p95": char_lens[int(0.95 * (len(char_lens) - 1))],
        "char_len_max": char_lens[-1],
    }
    (out_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def build_corpus(bench: str, raw_dir: Path, out_dir: Path, *,
                 from_samples: bool = False, samples_dir: Path | None = None,
                 max_chars: int = 1800, overlap: int = 200) -> dict:
    records = load_dev(bench, Path(raw_dir))
    mode = "full_dev"
    if from_samples:
        mode = "samples"
        ids = _sample_ids(bench, Path(samples_dir or Path(raw_dir).parent / "samples"))
        records = [r for r in records if r["id"] in ids]
    return build_corpus_from_records(bench, records, out_dir, mode=mode,
                                     max_chars=max_chars, overlap=overlap)


def _sample_ids(bench: str, samples_dir: Path) -> set[str]:
    files = sorted(Path(samples_dir).glob(f"{bench}_dev_n*_seed*.jsonl"))
    if not files:
        raise FileNotFoundError(f"{samples_dir} 下没有 {bench} 的抽样清单")
    ids = set()
    with open(files[-1], encoding="utf-8") as f:
        for line in f:
            if line.strip():
                ids.add(json.loads(line)["id"])
    return ids
