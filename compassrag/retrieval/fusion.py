"""多路检索结果融合：RRF（倒数排名融合，免调参）+ chunk_id 精确去重 + 近重语义合并。

近重合并（余弦 ≥0.92，② 组件）在向量通路可用后由 agent 启用。
"""

from collections import defaultdict

RRF_K_DEFAULT = 60
NEAR_DUP_THRESHOLD_DEFAULT = 0.92


def rrf_fuse(routes: list[list[str]], k: int = RRF_K_DEFAULT, top_n: int | None = None,
             weights: list[float] | None = None) -> list[tuple[str, float]]:
    """routes = 各路检索的 id 有序排名列表；返回 [(id, rrf_score)] 按融合分降序。

    得分 = Σ w_i/(k + rank)，rank 从 1 计；weights 为各路票权（默认全 1）——
    质量差的路配低权重，避免其头部块与优质路同权挤占名额。
    同分按首见顺序稳定排序。
    """
    if weights is not None:
        assert len(weights) == len(routes), f"权重数 {len(weights)} != 路数 {len(routes)}"
    scores: dict[str, float] = defaultdict(float)
    first_seen: dict[str, int] = {}
    for ri, route in enumerate(routes):
        w = 1.0 if weights is None else float(weights[ri])
        for rank, cid in enumerate(route, start=1):
            scores[cid] += w / (k + rank)
            first_seen.setdefault(cid, len(first_seen))
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return ordered[:top_n] if top_n is not None else ordered


def dedup_by_id(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def near_dup_merge(ranked: list[tuple[str, float]], vector_fn,
                   threshold: float = NEAR_DUP_THRESHOLD_DEFAULT) -> list[tuple[str, float]]:
    """余弦近重合并（②）：保序保留每组排名最高的块；向量缺失的块按不重复处理。

    vector_fn(chunk_id) -> np.ndarray | None；每个候选只与已保留的块比较。
    """
    kept: list[tuple[str, float]] = []
    kept_vecs: list = []
    for cid, score in ranked:
        v = vector_fn(cid)
        if v is None:
            kept.append((cid, score))
            continue
        if any(float(v @ kv) >= threshold for kv in kept_vecs):
            continue
        kept.append((cid, score))
        kept_vecs.append(v)
    return kept
