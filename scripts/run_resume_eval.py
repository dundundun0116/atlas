#!/usr/bin/env python3
"""可复现的正式评测（Task 6）：五组配置 × N 次重复，产出均值/波动 + 逐题 trace。

为什么单独一个脚本：`eval_qa.py` 只跑单配置，而"简历级"结论需要
**同一题集、同一索引、同一预算、多次重复**，并且每次都要留下 manifest 指纹。

用法：
  python scripts/run_resume_eval.py --dry-run              # 只打印将要跑什么
  python scripts/run_resume_eval.py --repeats 2 --strict-answer-words 5
  python scripts/run_resume_eval.py --report-only          # 只汇总已有结果

产物：runs/eval/r{n}/{bench}__{config}.jsonl + traces/ + manifests/，
汇总写到 runs/eval/summary.json 并打印表格。
"""

import argparse
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CONFIGS = ("naive", "plus_rewrite", "plus_kg", "plus_decision", "plus_verify")


def _result_rows(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(row, dict) or not row.get("id"):
            return None
        rows.append(row)
    return rows


def _result_complete(target: Path, manifest: Path, expected_ids: set[str],
                     *, benchmark: str, config_name: str, variant: str,
                     expected_config: dict) -> bool:
    """Skip only when rows and run identity exactly match this request."""
    if not expected_ids:
        return False
    rows = _result_rows(target)
    if rows is None:
        return False
    ids = [str(row["id"]) for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        return False
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    from compassrag.eval.manifest import sha1_ids

    return (
        data.get("benchmark") == benchmark
        and data.get("config_name") == config_name
        and data.get("corpus_variant") == variant
        and int(data.get("n_questions", -1)) == len(expected_ids)
        and data.get("sample_hash") == sha1_ids(expected_ids, prefix=f"{benchmark}:")
        and data.get("config") == expected_config
    )


def _expected_ids(args) -> set[str]:
    from compassrag.corpus.benchmarks import load_dev
    from compassrag.corpus.sample import stratified_subset

    raw_dir = Path(args.raw_dir)
    sample_dir = Path(args.samples_dir)
    records = load_dev(args.benchmark, raw_dir)
    sample_file = sorted(sample_dir.glob(f"{args.benchmark}_dev_n*_seed*.jsonl"))[-1]
    sample_ids = {
        json.loads(line)["id"] for line in sample_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    sampled = [record for record in records if record["id"] in sample_ids]
    if args.limit:
        sampled = stratified_subset(sampled, args.limit)
    return {str(record["id"]) for record in sampled}


def _expected_config(args, cfg: str) -> dict:
    from compassrag.agent import AgentConfig

    raw = yaml.safe_load((REPO_ROOT / "configs" / "ablation" / f"{cfg}.yaml").read_text())
    config = AgentConfig(**raw["agent"])
    config.use_graph = args.use_graph
    if args.strict_answer_words:
        config.strict_answer_words = args.strict_answer_words
    return config.snapshot()


def _run_one(cfg: str, args, repeat_dir: Path) -> Path:
    suffix = "" if args.variant == "full_dev" else f"__{args.variant}"
    out = repeat_dir / f"{args.benchmark}__{cfg}{suffix}.jsonl"
    cmd = [
        sys.executable, "-u", str(REPO_ROOT / "scripts" / "eval_qa.py"),
        "--benchmarks", args.benchmark, "--variant", args.variant,
        "--config", str(REPO_ROOT / "configs" / "ablation" / f"{cfg}.yaml"),
        "--limit", str(args.limit),
        "--out-dir", str(repeat_dir),
        "--trace-dir", str(repeat_dir / "traces"),
        "--manifest-dir", str(repeat_dir / "manifests"),
    ]
    if args.strict_answer_words:
        cmd += ["--strict-answer-words", str(args.strict_answer_words)]
    log = repeat_dir / f"{cfg}.log"
    with log.open("w") as fh:
        subprocess.run(cmd, check=True, stdout=fh, stderr=subprocess.STDOUT)
    return out


def summarize_repeat(repeat_dir: Path, benchmark: str, configs, variant: str = "full_dev") -> dict:
    from compassrag.eval.report import load_rows, summarize

    suffix = "" if variant == "full_dev" else f"__{variant}"
    out = {}
    for cfg in configs:
        rows = load_rows(repeat_dir / f"{benchmark}__{cfg}{suffix}.jsonl")
        if not rows:
            continue
        s = summarize(rows)
        out[cfg] = {
            "n": s["n"],
            "em_strict": s["quality"]["em"],
            "em_relaxed": s["quality"]["relaxed_em"],
            "f1": s["quality"]["f1"],
            "ev_recall": s["evidence"]["ev_recall"],
            "ev_full_hit": s["evidence"]["ev_full_hit"],
            "claim_support_rate": s["evidence"]["claim_support_rate"],
            "tokens_per_q": s["cost"]["tokens_per_q"],
            "rounds_per_q": s["cost"]["rounds_per_q"],
            "refused_rate": s["reliability"]["refused_rate"],
            "repair_success_rate": s["reliability"]["repair_success_rate"],
            "stop_reasons": s["reliability"]["stop_reasons"],
        }
    return out


def aggregate(per_repeat: list[dict], configs) -> dict:
    agg = {}
    for cfg in configs:
        rows = [r[cfg] for r in per_repeat if cfg in r]
        if not rows:
            continue
        item = {"repeats": len(rows)}
        for key in ("em_strict", "em_relaxed", "f1", "ev_recall", "ev_full_hit",
                    "tokens_per_q", "rounds_per_q", "refused_rate"):
            values = [float(r.get(key) or 0.0) for r in rows]
            item[f"{key}_mean"] = round(statistics.mean(values), 4)
            item[f"{key}_std"] = round(statistics.stdev(values), 4) if len(values) > 1 else 0.0
        agg[cfg] = item
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark", default="musique")
    ap.add_argument("--variant", default="samples")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS))
    ap.add_argument("--strict-answer-words", type=int, default=0,
                    help=">0 时统一加答案词数约束（建议 5，见 decisions/D11）")
    ap.add_argument("--out-root", default=REPO_ROOT / "runs" / "eval")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--parallel", type=int, default=1,
                    help="同时跑几个配置（受内存与上游限流约束，建议 2–3）")
    ap.add_argument("--raw-dir", default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--samples-dir", default=REPO_ROOT / "data" / "samples")
    ap.add_argument("--no-graph", dest="use_graph", action="store_false")
    ap.set_defaults(use_graph=True)
    args = ap.parse_args()
    configs = tuple(c.strip() for c in args.configs.split(",") if c.strip())

    root = Path(args.out_root)
    if args.dry_run:
        for r in range(1, args.repeats + 1):
            for cfg in configs:
                print(f"[dry-run] repeat {r} · {args.benchmark}/{args.variant} · {cfg} "
                      f"· limit={args.limit} · words={args.strict_answer_words or '-'}")
        print(f"[dry-run] 产物目录：{root}")
        return 0

    jobs: list[tuple[int, str, Path]] = []
    expected_ids = _expected_ids(args) if not args.report_only else set()
    for r in range(1, args.repeats + 1):
        repeat_dir = root / f"r{r}"
        repeat_dir.mkdir(parents=True, exist_ok=True)
        for cfg in configs:
            suffix = "" if args.variant == "full_dev" else f"__{args.variant}"
            target = repeat_dir / f"{args.benchmark}__{cfg}{suffix}.jsonl"
            manifest = repeat_dir / "manifests" / f"{args.benchmark}__{cfg}.json"
            if _result_complete(target, manifest, expected_ids,
                                benchmark=args.benchmark, config_name=cfg,
                                variant=args.variant,
                                expected_config=_expected_config(args, cfg)):
                print(f"[r{r}] {cfg} 已有结果，跳过（续跑）")
                continue
            jobs.append((r, cfg, repeat_dir))

    if not args.report_only and jobs:
        def work(job):
            r, cfg, repeat_dir = job
            print(f"[r{r}] 跑 {cfg} …", flush=True)
            _run_one(cfg, args, repeat_dir)
            print(f"[r{r}] {cfg} 完成", flush=True)

        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
            list(pool.map(work, jobs))

    per_repeat = [summarize_repeat(root / f"r{r}", args.benchmark, configs, args.variant)
                  for r in range(1, args.repeats + 1)]

    summary = {"benchmark": args.benchmark, "variant": args.variant,
               "limit": args.limit, "repeats": args.repeats,
               "strict_answer_words": args.strict_answer_words,
               "per_repeat": per_repeat, "aggregate": aggregate(per_repeat, configs)}
    out = root / "summary.json"
    root.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'配置':<16}{'EM(mean±std)':>16}{'F1':>8}{'召回':>8}{'tok/q':>9}{'轮':>6}")
    for cfg, a in summary["aggregate"].items():
        print(f"{cfg:<16}{a['em_strict_mean']:>8.3f}±{a['em_strict_std']:<6.3f}"
              f"{a['f1_mean']:>8.3f}{a['ev_recall_mean']:>8.3f}"
              f"{a['tokens_per_q_mean']:>9.0f}{a['rounds_per_q_mean']:>6.2f}")
    print(f"\n汇总写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
