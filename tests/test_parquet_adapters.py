from compassrag.corpus.benchmarks import (
    adapt_parquet_row,
    normalize_2wiki,
)


HOTPOT_PARQUET_ROW = {
    "id": "5a8b",
    "question": "Were Scott Derrickson and Ed Wood of the same nationality?",
    "answer": "yes",
    "type": "comparison",
    "level": "hard",
    "supporting_facts": {"title": ["Scott Derrickson", "Ed Wood"], "sent_id": [0, 0]},
    "context": {"title": ["Ed Wood (film)", "Scott Derrickson"],
                "sentences": [["Ed Wood is a 1994 film.", "It was directed by Tim Burton."],
                              ["Scott Derrickson is an American director."]]},
}


def test_adapt_hotpot_row_to_legacy_shape():
    item = adapt_parquet_row("hotpotqa", HOTPOT_PARQUET_ROW)
    assert item["_id"] == "5a8b" and item["level"] == "hard" and item["type"] == "comparison"
    assert item["context"][0] == ("Ed Wood (film)", ["Ed Wood is a 1994 film.", "It was directed by Tim Burton."])
    assert item["supporting_facts"] == {"title": ["Scott Derrickson", "Ed Wood"], "sent_id": [0, 0]}
    normalized = __import__("compassrag.corpus.benchmarks", fromlist=["normalize_hotpotqa"]).normalize_hotpotqa(item)
    assert normalized["stratum"] == "hard"
    assert normalized["type"] == "comparison"


def test_normalize_2wiki_type_beats_count_for_stratum():
    item = {"_id": "w1", "type": "comparison", "question": "Q", "answer": "C",
            "context": [["T1", ["a."]], ["T2", ["b."]]],
            "supporting_facts": {"title": ["T1", "T2"], "sent_id": [0, 0]}}
    assert normalize_2wiki(item)["stratum"] == "comparison"
    item.pop("type")
    assert normalize_2wiki(item)["stratum"] == "2"
