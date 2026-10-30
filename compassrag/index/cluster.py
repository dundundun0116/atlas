"""语料聚类（本地、无 API）：k-means 分簇 + 每簇代表块采样。

向量用 BGE-M3 语料向量（已归一化）：k-means 在余弦空间分簇；
代表块 = 距簇质心最近的一半 + 其余沿相似度序均匀跨越（覆盖簇内广度），
全部按行号稳定取值，重复构建结果一致。
"""

import numpy as np

KMEANS_SEED_DEFAULT = 42
CHUNKS_PER_ENTRY_DEFAULT = 16
ENTRY_PER_CHUNKS_DEFAULT = 100  # 默认簇数 = 块数 / 100


def default_k(n_chunks: int) -> int:
    return max(1, n_chunks // ENTRY_PER_CHUNKS_DEFAULT)


def kmeans_labels(vectors: np.ndarray, k: int, *, seed: int = KMEANS_SEED_DEFAULT) -> np.ndarray:
    from sklearn.cluster import KMeans

    k = max(1, min(int(k), vectors.shape[0]))
    km = KMeans(n_clusters=k, random_state=seed, n_init=4)
    return km.fit_predict(vectors)


def sample_representatives(vectors: np.ndarray, member_idx, n_take: int) -> list[int]:
    """簇内取代表块行号：最近质心的一半 + 其余均匀跨越；不足 n_take 则全取。"""
    idx = np.asarray(sorted(int(i) for i in member_idx))
    if idx.size <= n_take:
        return [int(i) for i in idx]
    centroid = vectors[idx].mean(axis=0)
    order = np.argsort(-(vectors[idx] @ centroid), kind="stable")
    head_n = (n_take + 1) // 2
    head = idx[order[:head_n]]
    rest = idx[order[head_n:]]
    tail_n = n_take - head_n
    if tail_n <= 0:
        return [int(i) for i in head]
    pos = np.linspace(0, rest.size - 1, tail_n).round().astype(int)
    return [int(i) for i in np.concatenate([head, rest[pos]])]
