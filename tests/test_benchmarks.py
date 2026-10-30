from compassrag.corpus.benchmarks import normalize_hotpotqa, normalize_musique, normalize_2wiki


HOTPOT_ITEM = {
    "_id": "h1",
    "level": "hard",
    "question": "Q?",
    "answer": "A",
    "context": [["Turmeric", ["Turmeric is a plant.", "It is used in cooking."]],
                 ["Cooking", ["Cooking is fun."]]],
    "supporting_facts": {"title": ["Turmeric", "Cooking"], "sent_id": [1, 0]},
}

MUSIQUE_ITEM = {
    "id": "m1",
    "question": "Q2?",
    "answer": "B",
    "paragraphs": [
        {"idx": 0, "title": "X", "paragraph_text": "para x.", "is_supporting": True},
        {"idx": 1, "title": "Y", "paragraph_text": "para y.", "is_supporting": False},
        {"idx": 2, "title": "Z", "paragraph_text": "para z.", "is_supporting": True},
    ],
}


def test_normalize_hotpotqa_dict_supporting():
    r = normalize_hotpotqa(HOTPOT_ITEM)
    assert r["id"] == "h1" and r["answer"] == "A" and r["stratum"] == "hard"
    paras = {p["title"]: p for p in r["paragraphs"]}
    assert paras["Turmeric"]["is_supporting"] is True
    assert paras["Cooking"]["is_supporting"] is True
    assert paras["Turmeric"]["text"].startswith("Turmeric is a plant.")


def test_normalize_hotpotqa_list_supporting():
    item = dict(HOTPOT_ITEM, supporting_facts=[["Turmeric", 1], ["Cooking", 0]])
    r = normalize_hotpotqa(item)
    assert all(p["is_supporting"] for p in r["paragraphs"])


def test_normalize_hotpotqa_missing_level_falls_back():
    item = {k: v for k, v in HOTPOT_ITEM.items() if k != "level"}
    assert normalize_hotpotqa(item)["stratum"] == "unknown"


def test_normalize_2wiki_stratum_counts_unique_supporting_titles():
    item = {"_id": "w1", "question": "Q", "answer": "C",
            "context": [["T1", ["a."]], ["T2", ["b."]], ["T3", ["c."]]],
            "supporting_facts": {"title": ["T1", "T1", "T2"], "sent_id": [0, 0, 0]}}
    r = normalize_2wiki(item)
    assert r["stratum"] == "2"
    assert r["paragraphs"][0]["is_supporting"] and r["paragraphs"][1]["is_supporting"]
    assert not r["paragraphs"][2]["is_supporting"]


def test_normalize_musique_stratum_counts_supporting():
    r = normalize_musique(MUSIQUE_ITEM)
    assert r["id"] == "m1" and r["stratum"] == "2"
    assert r["paragraphs"][0]["is_supporting"] and not r["paragraphs"][1]["is_supporting"]
    assert r["paragraphs"][2]["text"] == "para z."
