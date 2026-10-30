import json

from compassrag.corpus.chunking import chunk_text, iter_chunks, normalize_ws


def test_normalize_ws_collapses_whitespace():
    assert normalize_ws("  a\n\n b\t c  ") == "a b c"


def test_short_text_single_chunk():
    assert chunk_text("hello world.", max_chars=100, overlap=20) == ["hello world."]


def _numbered_sentences(n, per=20):
    return " ".join(f"Sentence number {i:03d} padding padding." for i in range(n))


def test_long_text_splits_with_overlap_and_bounds():
    text = _numbered_sentences(30)
    chunks = chunk_text(text, max_chars=120, overlap=45)
    assert len(chunks) > 2
    assert all(len(c) <= 120 for c in chunks)
    # 全部句子都被覆盖
    assert all(f"Sentence number {i:03d}" in " ".join(chunks) for i in range(30))
    # 相邻窗口有重叠：后窗的首句出现在前窗中
    first_of_next = chunks[1].split(". ")[0]
    assert first_of_next in chunks[0]


def test_pathological_no_punctuation_hard_split():
    text = "a" * 5000
    chunks = chunk_text(text, max_chars=1800, overlap=200)
    assert all(len(c) <= 1800 for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks) == "a" * 5000


def test_chunk_id_stable_and_distinct():
    from compassrag.corpus.chunking import chunk_id
    assert chunk_id("T", "abc") == chunk_id("T", "abc")
    assert chunk_id("T", "abc") != chunk_id("T2", "abc")
    assert len(chunk_id("T", "abc")) == 16


def test_iter_chunks_dedups_shared_paragraphs():
    para = {"title": "Shared", "text": "Same text here.", "is_supporting": True}
    records = [
        {"id": "r1", "paragraphs": [para, {"title": "Only1", "text": "one.", "is_supporting": False}]},
        {"id": "r2", "paragraphs": [para]},
    ]
    chunks = list(iter_chunks(records, "benchX"))
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids)) == 2
    shared = [c for c in chunks if c["title"] == "Shared"][0]
    assert shared["benchmark"] == "benchX"
    # 同一段落可对甲题是支撑、对乙题是干扰：块上不得固定 is_supporting，
    # 证据归属由评测期按题的支撑标题 join 决定；question_id 仅作首见溯源。
    assert "is_supporting" not in shared
    assert "question_id" in shared
