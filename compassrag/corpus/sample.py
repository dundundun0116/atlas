"""分层比例抽样：固定 seed、最大余数法分配，输出可复现的抽样清单。"""

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path


def stratified_sample(records: list[dict], n_total: int, seed: int = 42,
                      key: str = "stratum") -> tuple[list[dict], dict[str, int]]:
    """按 stratum 比例抽 n_total 条，返回 (样本, 各层分配数)。

    确定性来源：先按 id 排序固定层内顺序，单一种子按层名字典序依次抽样。
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x["id"]):
        groups[r[key]].append(r)
    strata = sorted(groups)
    sizes = {s: len(groups[s]) for s in strata}
    total = sum(sizes.values())
    n_total = min(n_total, total)

    # 最大余数法：floor 分配 + 余数按小数部分降序（平局按层名）补齐
    raw = {s: n_total * sizes[s] / total for s in strata}
    alloc = {s: int(raw[s]) for s in strata}
    leftover = n_total - sum(alloc.values())
    by_frac = sorted(strata, key=lambda s: (-(raw[s] - alloc[s]), s))
    for s in by_frac[:leftover]:
        alloc[s] += 1

    rng = random.Random(seed)
    picked: list[dict] = []
    for s in strata:
        picked.extend(rng.sample(groups[s], alloc[s]))
    picked.sort(key=lambda r: r["id"])
    return picked, alloc


def make_record_stub(benchmark: str, record: dict) -> dict:
    """抽样清单只存 id 与分层键（题面在原始 dev 中，按 id 关联）。"""
    return {"benchmark": benchmark, "id": record["id"], "stratum": record["stratum"]}


def stratified_subset(records: list[dict], n_total: int, key: str = "stratum") -> list[dict]:
    """从既有抽样清单里取分层比例子集（评测"能子集不跑全量"用）。

    按层比例（最大余数法）取每层清单顺序的前若干条——不重新抽样，
    因此子集是全量的前缀式收敛：先跑子集、后补全量时已完成行天然复用（按 id 续跑）。
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r[key]].append(r)
    strata = sorted(groups)
    total = sum(len(groups[s]) for s in strata)
    n_total = min(n_total, total)
    raw = {s: n_total * len(groups[s]) / total for s in strata}
    alloc = {s: min(int(raw[s]), len(groups[s])) for s in strata}
    leftover = n_total - sum(alloc.values())
    by_frac = sorted(strata, key=lambda s: (-(raw[s] - alloc[s]), s))
    for s in by_frac:
        if leftover <= 0:
            break
        if alloc[s] < len(groups[s]):
            alloc[s] += 1
            leftover -= 1
    out: list[dict] = []
    for s in strata:
        out.extend(groups[s][:alloc[s]])
    return out


def write_samples(benchmark: str, picked: list[dict], allocation: dict[str, int], *,
                  seed: int, out_path: Path, n_population: int,
                  source_path: Path) -> dict:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = [make_record_stub(benchmark, r) for r in picked]
    out_path.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in stubs) + "\n",
        encoding="utf-8")
    manifest = {
        "benchmark": benchmark,
        "seed": seed,
        "n_population": n_population,
        "n_sampled": len(picked),
        "allocation": allocation,
        "source_file": Path(source_path).name,
        "source_sha256": _sha256(Path(source_path)),
        "output_file": out_path.name,
        "output_sha256": _sha256(out_path),
    }
    Path(str(out_path).replace(".jsonl", "_manifest.json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
