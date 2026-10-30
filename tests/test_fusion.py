import pytest

from compassrag.retrieval.fusion import dedup_by_id, rrf_fuse


def test_rrf_prefers_multi_route_consensus():
    routes = [
        ["a", "b", "c"],   # 路线1排名
        ["b", "d", "a"],   # 路线2排名
    ]
    fused = rrf_fuse(routes, k=60, top_n=3)
    ids = [x for x, _ in fused]
    # b 在两路都靠前：1/61 + 1/61 > a 的 1/61 + 1/63
    assert ids[0] == "b"
    assert "a" in ids and len(ids) == 3
    assert all(score > 0 for _, score in fused)


def test_rrf_deterministic_on_ties():
    routes = [["x"], ["y"]]  # 两路第一名得分相同
    a = rrf_fuse(routes, k=60, top_n=2)
    b = rrf_fuse(routes, k=60, top_n=2)
    assert [x for x, _ in a] == [x for x, _ in b] == ["x", "y"]


def test_rrf_single_route_keeps_order():
    fused = rrf_fuse([["c", "a", "b"]], k=60, top_n=2)
    assert [x for x, _ in fused] == ["c", "a"]


def test_rrf_weights_downgrade_weak_route():
    # 弱路（低权重）的头名挤不过强路的共识：b = 1/62 + 0.2/61 ≈ 0.0194 > weak = 1/61
    routes = [["weak"], ["a", "b"], ["b", "c"]]
    fused = rrf_fuse(routes, k=60, top_n=3, weights=[1.0, 1.0, 0.2])
    assert [x for x, _ in fused][0] == "b"
    # 默认（无权重）行为不变：b 为两路共识最高分；weak 与 a 同分 1/61，按首见顺序 weak 在前
    assert [x for x, _ in rrf_fuse(routes, k=60, top_n=3)] == ["b", "weak", "a"]
    with pytest.raises(AssertionError):
        rrf_fuse(routes, weights=[1.0, 1.0])  # 权重数必须与路数一致


def test_dedup_by_id_keeps_first_occurrence_order():
    out = dedup_by_id(["a", "b", "a", "c", "b"])
    assert out == ["a", "b", "c"]
