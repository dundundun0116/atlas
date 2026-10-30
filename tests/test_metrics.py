from compassrag.eval.metrics import (
    em_score,
    f1_score,
    full_hit_at_k,
    recall_at_k,
    squad_normalize,
    best_over_golds,
)


def test_squad_normalize_lowercases_strips_punct_articles():
    assert squad_normalize("The  Fox, and the Cat!") == "fox and cat"  # 只去冠词 a/an/the
    assert squad_normalize("Denver Broncos") == "denver broncos"


def test_em_exact_match_case_and_punct_insensitive():
    assert em_score("Denver Broncos", "denver broncos") == 1.0
    assert em_score("Denver Broncos.", "denver broncos") == 1.0
    assert em_score("Denver Broncos", "Green Bay Packers") == 0.0


def test_f1_partial_overlap():
    assert f1_score("Denver Broncos", "denver broncos") == 1.0
    # precision 2/3, recall 2/2 -> f1 = 0.8
    assert abs(f1_score("denver broncos", "denver broncos defense") - 0.8) < 1e-9


def test_best_over_golds_takes_max():
    golds = ["wrong answer", "denver broncos"]
    assert best_over_golds("Denver Broncos", golds, em_score) == 1.0
    # 预测多一个词 "defense"：对第二个 gold 的 f1 = 0.8（见 test_f1_partial_overlap）
    assert abs(best_over_golds("denver broncos defense", golds, f1_score) - 0.8) < 1e-9


def test_recall_at_k_counts_gold_titles_in_topk():
    retrieved = ["T1", "T2", "T3", "T4", "T5"]
    gold = {"T2", "T5", "T9"}
    assert abs(recall_at_k(retrieved, gold, k=5) - 2 / 3) < 1e-9
    assert recall_at_k(retrieved, gold, k=1) == 0.0


def test_full_hit_at_k_requires_all_gold():
    retrieved = ["T1", "T2", "T3", "T4", "T5"]
    assert full_hit_at_k(retrieved, {"T1", "T2"}, k=2) == 1.0
    assert full_hit_at_k(retrieved, {"T1", "T2"}, k=1) == 0.0
    assert full_hit_at_k(retrieved, {"T9"}, k=5) == 0.0
