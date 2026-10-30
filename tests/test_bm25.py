from compassrag.retrieval.bm25 import BM25Index

CHUNKS = [
    {"chunk_id": "c0", "title": "Turmeric", "text": "Turmeric is a spice plant used in cooking."},
    {"chunk_id": "c1", "title": "Cooking", "text": "Cooking is the art of preparing food."},
    {"chunk_id": "c2", "title": "Astronomy", "text": "Astronomy studies stars and planets."},
]


def test_build_and_search_ranks_relevant_first():
    idx = BM25Index.build(CHUNKS)
    hits = idx.search("turmeric spice plant", k=2)
    assert hits[0][0] == "c0"
    assert len(hits) == 2
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)


def test_index_titles_and_text_both_searchable():
    idx = BM25Index.build(CHUNKS)
    assert idx.search("Cooking", k=1)[0][0] == "c1"


def test_save_load_roundtrip(tmp_path):
    idx = BM25Index.build(CHUNKS)
    idx.save(tmp_path)
    loaded = BM25Index.load(tmp_path)
    assert loaded.search("turmeric spice plant", k=1)[0][0] == "c0"


def test_search_k_larger_than_corpus():
    idx = BM25Index.build(CHUNKS)
    assert len(idx.search("cooking food", k=100)) == 3
