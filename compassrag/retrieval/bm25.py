"""BM25 关键词检索（bm25s 库封装）：对 chunks.jsonl 建索引 / 保存 / 加载 / 检索。

索引文本 = "title\\ntext"——标题里的实体名对 BM25 命中率至关重要。
ordinal ↔ chunk 的对应关系由构建时的 chunks 顺序决定，保存与加载一致。
"""

from pathlib import Path

import bm25s


class BM25Index:
    def __init__(self, retriever: bm25s.BM25, chunk_ids: list[str]):
        self._retriever = retriever
        self.chunk_ids = chunk_ids

    @classmethod
    def build(cls, chunks: list[dict], *, text_fn=None) -> "BM25Index":
        """text_fn 默认取 (title, text) 拼接；chunks 顺序即索引 ordinal。"""
        if text_fn is None:
            def text_fn(c):
                return f"{c['title']}\n{c['text']}"
        corpus_tokens = bm25s.tokenize([text_fn(c) for c in chunks], stopwords="en", show_progress=False)
        retriever = bm25s.BM25()
        retriever.index(corpus_tokens, show_progress=False)
        return cls(retriever, [c["chunk_id"] for c in chunks])

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        k = min(k, len(self.chunk_ids))  # bm25s 要求 k ≤ 语料量
        q_tokens = bm25s.tokenize(query, stopwords="en", show_progress=False)
        results, scores = self._retriever.retrieve(q_tokens, k=k, show_progress=False)
        return [(self.chunk_ids[int(results[0, i])], float(scores[0, i]))
                for i in range(results.shape[1])]

    def save(self, dir_path: Path) -> None:
        import json
        Path(dir_path).mkdir(parents=True, exist_ok=True)
        self._retriever.save(str(dir_path), corpus=None)
        (Path(dir_path) / "chunk_ids.json").write_text(
            json.dumps(self.chunk_ids, ensure_ascii=False))

    @classmethod
    def load(cls, dir_path: Path) -> "BM25Index":
        import json
        retriever = bm25s.BM25.load(str(dir_path))
        chunk_ids = json.loads((Path(dir_path) / "chunk_ids.json").read_text())
        return cls(retriever, chunk_ids)
