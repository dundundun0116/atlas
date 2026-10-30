import json

from compassrag.corpus.build import build_corpus_from_records

RECORDS = [
    {"id": "r1", "paragraphs": [{"title": "T1", "text": "alpha beta.", "is_supporting": True}]},
    {"id": "r2", "paragraphs": [{"title": "T1", "text": "alpha beta.", "is_supporting": False}]},
]


def test_mode_encoded_dirs_no_overwrite(tmp_path):
    s = build_corpus_from_records("b1", RECORDS, tmp_path, mode="samples")
    f = build_corpus_from_records("b1", RECORDS, tmp_path, mode="full_dev")
    assert (tmp_path / "b1__samples" / "chunks.jsonl").exists()
    assert (tmp_path / "b1__full_dev" / "chunks.jsonl").exists()
    assert s["mode"] == "samples" and f["mode"] == "full_dev"


def test_stats_fields_no_supporting_ownership(tmp_path):
    s = build_corpus_from_records("b1", RECORDS, tmp_path, mode="full_dev")
    assert s["n_questions"] == 2
    assert s["n_unique_paragraphs"] == 1  # 同段落跨题去重
    assert s["n_chunks"] == 1
    assert "n_supporting_chunks" not in s  # 证据归属是按题的，块上没有这个字段
    back = json.loads((tmp_path / "b1__full_dev" / "stats.json").read_text())
    assert back["mode"] == "full_dev"
