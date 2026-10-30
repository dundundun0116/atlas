import numpy as np

from compassrag.index.cluster import default_k, kmeans_labels, sample_representatives


def test_default_k_is_chunks_over_100():
    assert default_k(21118) == 211
    assert default_k(50) == 1


def _two_groups():
    """两簇明显分离的 8 个向量（已归一化的近似形式即可）。"""
    a = np.array([[1.0, 0.0], [1.0, 0.02], [0.98, -0.02], [1.0, 0.01]], dtype=np.float32)
    b = np.array([[-1.0, 0.0], [-1.0, 0.03], [-0.99, 0.0], [-1.0, -0.01]], dtype=np.float32)
    return np.vstack([a, b])


def test_kmeans_labels_separates_obvious_groups_and_is_deterministic():
    v = _two_groups()
    l1 = kmeans_labels(v, 2, seed=42)
    l2 = kmeans_labels(v, 2, seed=42)
    assert np.array_equal(l1, l2)
    assert len(set(l1[:4])) == 1 and len(set(l1[4:])) == 1 and l1[0] != l1[4]


def test_kmeans_k_clamped_to_n():
    v = _two_groups()
    labels = kmeans_labels(v, 99, seed=42)
    assert len(labels) == 8


def test_sample_representatives_prefers_centroid_and_covers_spread():
    v = _two_groups()
    members = list(range(8))
    rows = sample_representatives(v, members, n_take=4)
    assert len(rows) == 4 and len(set(rows)) == 4
    assert all(0 <= r < 8 for r in rows)
    # 前一半应是距质心最近的块：全取时至少包含组内最中心的那一个
    assert 0 in rows or 4 in rows


def test_sample_representatives_returns_all_when_cluster_smaller():
    v = _two_groups()
    assert sorted(sample_representatives(v, [1, 3, 5], n_take=10)) == [1, 3, 5]
