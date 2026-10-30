import hashlib
import json

from compassrag.corpus.sample import (
    make_record_stub,
    stratified_sample,
    stratified_subset,
    write_samples,
)


def _pop(strata_counts):
    records = []
    for stratum, count in strata_counts.items():
        for i in range(count):
            records.append({"id": f"{stratum}-{i:04d}", "stratum": stratum,
                            "question": "q", "answer": "a"})
    return records


def test_allocation_proportional_18_9_3():
    records = _pop({"A": 60, "B": 30, "C": 10})
    picked, alloc = stratified_sample(records, 30, seed=42)
    assert alloc == {"A": 18, "B": 9, "C": 3}
    assert len(picked) == 30


def test_same_seed_same_sample():
    records = _pop({"A": 60, "B": 30, "C": 10})
    ids_a = [r["id"] for r in stratified_sample(records, 30, seed=42)[0]]
    ids_b = [r["id"] for r in stratified_sample(records, 30, seed=42)[0]]
    assert ids_a == ids_b


def test_different_seed_different_sample():
    records = _pop({"A": 60, "B": 30, "C": 10})
    ids_a = [r["id"] for r in stratified_sample(records, 30, seed=42)[0]]
    ids_b = [r["id"] for r in stratified_sample(records, 30, seed=7)[0]]
    assert ids_a != ids_b


def test_single_stratum_returns_random_n():
    records = _pop({"hard": 100})
    picked, alloc = stratified_sample(records, 30, seed=42)
    assert len(picked) == 30 and alloc == {"hard": 30}


def test_n_clamped_to_population():
    records = _pop({"A": 5})
    picked, alloc = stratified_sample(records, 50, seed=42)
    assert len(picked) == 5


def test_write_samples_output_and_manifest(tmp_path):
    records = _pop({"A": 60, "B": 40})
    picked, alloc = stratified_sample(records, 20, seed=42)
    src = tmp_path / "source.json"
    src.write_text("[]")
    out = tmp_path / "sample.jsonl"
    manifest = write_samples("benchX", picked, alloc, seed=42, out_path=out,
                             n_population=len(records), source_path=src)
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(lines) == 20
    assert set(lines[0].keys()) == {"benchmark", "id", "stratum"}
    assert lines[0]["benchmark"] == "benchX"
    assert manifest["allocation"] == alloc
    assert manifest["n_population"] == 100 and manifest["n_sampled"] == 20
    assert manifest["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert manifest["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_make_record_stub_fields():
    stub = make_record_stub("hotpotqa", {"id": "x", "stratum": "hard"})
    assert stub == {"benchmark": "hotpotqa", "id": "x", "stratum": "hard"}


def test_stratified_subset_proportional_and_nested():
    records = _pop({"A": 60, "B": 30, "C": 10})
    sub30 = stratified_subset(records, 30)
    counts = {}
    for r in sub30:
        counts[r["stratum"]] = counts.get(r["stratum"], 0) + 1
    assert counts == {"A": 18, "B": 9, "C": 3}
    # 前缀式收敛：小子集是大子集的子集（先跑子集、后补全量时已完成行可复用）
    small = {r["id"] for r in stratified_subset(records, 10)}
    big = {r["id"] for r in stratified_subset(records, 30)}
    assert small <= big


def test_stratified_subset_clamps_and_keeps_layer_order():
    records = _pop({"A": 5, "B": 3})
    assert len(stratified_subset(records, 100)) == 8
    sub = stratified_subset(records, 4)
    assert [r["id"] for r in sub] == ["A-0000", "A-0001", "A-0002", "B-0000"]
