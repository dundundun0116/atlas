import json

import numpy as np
import pytest

from compassrag.retrieval.dense import DenseIndex, embed_shards, load_embeddings, plan_shards


class FakeEmbedder:
    """确定性假编码器：按文本内容取全局下标 i → [i, i+0.5, -i, 1.0]（可跨片断言）。"""

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        rows = []
        for t in texts:
            i = int(t[1:].split("\n")[0])  # "T12\nt12" → 12
            rows.append([float(i), i + 0.5, -float(i), 1.0])
        return np.array(rows, dtype=np.float32)


def _chunks(n):
    return [{"chunk_id": f"c{i}", "title": f"T{i}", "text": f"t{i}"} for i in range(n)]


def test_plan_shards_covers_all_with_short_tail():
    assert plan_shards(12000, 5000) == [(0, 0, 5000), (1, 5000, 10000), (2, 10000, 12000)]
    assert plan_shards(3, 5000) == [(0, 0, 3)]
    assert plan_shards(0, 100) == []


def test_embed_shards_writes_all_and_resumes(tmp_path):
    emb = FakeEmbedder()
    chunks = _chunks(7)
    done = embed_shards(chunks, tmp_path, emb, shard_size=3, log=lambda *a: None)
    assert done == 3
    assert len(emb.calls) == 3
    # 再跑一遍：全部已存在，零新计算
    emb2 = FakeEmbedder()
    done2 = embed_shards(chunks, tmp_path, emb2, shard_size=3, log=lambda *a: None)
    assert done2 == 0 and emb2.calls == []
    # 删掉中间一片：只补那一片
    (tmp_path / "shard_001.npy").unlink()
    emb3 = FakeEmbedder()
    done3 = embed_shards(chunks, tmp_path, emb3, shard_size=3, log=lambda *a: None)
    assert done3 == 1 and len(emb3.calls) == 1 and emb3.calls[0] == ["T3\nt3", "T4\nt4", "T5\nt5"]


def test_load_embeddings_stitches_in_order(tmp_path):
    chunks = _chunks(7)
    embed_shards(chunks, tmp_path, FakeEmbedder(), shard_size=3, log=lambda *a: None)
    mat = load_embeddings(tmp_path, n_items=7, shard_size=3)
    assert mat.shape == (7, 4)
    assert mat[0].tolist() == [0.0, 0.5, 0.0, 1.0]
    assert mat[6].tolist() == [6.0, 6.5, -6.0, 1.0]


class DictEmbedder:
    """按文本查表返回向量的假编码器（正交向量便于断言余弦排序）。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return np.array([self.mapping[t] for t in texts], dtype=np.float32)


def _write_meta(tmp_path, n_chunks, shard_size):
    (tmp_path / "meta.json").write_text(
        json.dumps({"n_chunks": n_chunks, "shard_size": shard_size}), encoding="utf-8")


def test_dense_index_search_orders_by_cosine(tmp_path):
    chunk_ids = ["c0", "c1", "c2", "c3"]
    vectors = [[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.6, 0.8, 0]]
    for i, v in enumerate(vectors):
        np.save(tmp_path / f"shard_{i:03d}.npy", np.array([v], dtype=np.float32))
    _write_meta(tmp_path, 4, 1)
    emb = DictEmbedder({"q": [0.5, 0.5, 0.0]})
    idx = DenseIndex.load(tmp_path, chunk_ids, embedder=emb)

    hits = idx.search("q", k=2)
    # c3 最高（0.7）；c0/c1 并列 0.5，按行号稳定排序 → c0 在前
    assert [cid for cid, _ in hits] == ["c3", "c0"]
    assert hits[0][1] == pytest.approx(0.7)
    assert emb.calls == [["q"]]


def test_dense_index_k_clamped_to_corpus_size(tmp_path):
    np.save(tmp_path / "shard_000.npy", np.array([[1, 0], [0, 1]], dtype=np.float32))
    _write_meta(tmp_path, 2, 2)
    idx = DenseIndex.load(tmp_path, ["c0", "c1"], embedder=DictEmbedder({"q": [1.0, 0.0]}))
    assert len(idx.search("q", k=10)) == 2


def test_dense_index_load_rejects_missing_meta_or_mismatched_corpus(tmp_path):
    with pytest.raises(FileNotFoundError, match="meta.json"):
        DenseIndex.load(tmp_path, ["c0"], embedder=DictEmbedder({}))

    np.save(tmp_path / "shard_000.npy", np.array([[1, 0]], dtype=np.float32))
    _write_meta(tmp_path, 5, 5)
    with pytest.raises(ValueError, match="不一致"):
        DenseIndex.load(tmp_path, ["c0"], embedder=DictEmbedder({}))
