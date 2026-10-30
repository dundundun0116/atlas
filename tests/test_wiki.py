import json

import numpy as np
import pytest

from compassrag.index.wiki import (
    WikiEntry,
    WikiIndex,
    build_entry_vectors,
    build_wiki,
    clean_see_also,
    compute_related,
    generate_entry,
    load_entries,
    parse_entry_json,
)
from compassrag.llm.client import LLMResponse
from compassrag.retrieval.dense import DenseIndex


class DictEmbedder:
    def __init__(self, mapping):
        self.mapping = mapping

    def encode(self, texts):
        return np.array([self.mapping[t] for t in texts], dtype=np.float32)


class EntryLLM:
    """固定返回条目的假 LLM（统计调用次数以验证断点续跑）。"""

    def __init__(self, payload=None):
        self.payload = payload or {"title": "Topic", "summary": "Summary.", "see_also": []}
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens})
        return LLMResponse(text=json.dumps(self.payload), model="fake", prompt_tokens=1,
                           completion_tokens=1, total_tokens=2, latency_ms=1.0, finish_reason="stop")


def test_parse_entry_json_tolerates_fences_and_noise():
    assert parse_entry_json(json.dumps({"title": "T", "summary": "S", "see_also": []}))["title"] == "T"
    fenced = "```json\n" + json.dumps({"title": "T", "summary": "S", "see_also": ["A"]}) + "\n```"
    assert parse_entry_json(fenced)["see_also"] == ["A"]
    noisy = "Sure, here it is: " + json.dumps({"title": "T", "summary": "S"}) + " Hope it helps!"
    assert parse_entry_json(noisy)["title"] == "T"
    assert parse_entry_json("") is None
    assert parse_entry_json('{"title": "T"}') is None          # 缺 summary
    assert parse_entry_json("not json at all") is None


def test_clean_see_also_filters_unknown_and_dedups():
    allowed = ["Alpha", "Gamma of Beta", "Beta"]
    out = clean_see_also(["Gamma of beta", "GAMMA OF BETA", "Unknown", "Alpha", "Beta", "Beta"], allowed)
    assert out == ["Gamma of Beta", "Alpha", "Beta"]


def _chunks(n):
    return [{"chunk_id": f"c{i}", "title": f"T{i}", "text": f"text {i}"} for i in range(n)]


def _vectors_two_groups(n=6):
    a = np.tile(np.array([1.0, 0.0], dtype=np.float32), (n // 2, 1))
    b = np.tile(np.array([-1.0, 0.0], dtype=np.float32), (n - n // 2, 1))
    return np.vstack([a, b]) + np.linspace(-0.02, 0.02, n, dtype=np.float32)[:, None]


def test_build_wiki_writes_entries_and_resumes(tmp_path):
    chunks = _chunks(6)
    vectors = _vectors_two_groups(6)
    out = tmp_path / "wiki"
    llm = EntryLLM({"title": "Topic", "summary": "S", "see_also": ["T0", "T9"]})

    stats = build_wiki(chunks, vectors, out, llm, k=2, log=lambda *a: None)
    assert stats["built"] == 2 and stats["failed"] == 0 and len(llm.calls) == 2
    entries = load_entries(out / "entries.jsonl")
    assert [e.entry_id for e in entries] == ["w0000", "w0001"]
    assert all(e.chunk_ids and len(e.chunk_ids) > 0 for e in entries)
    # see_also 只能取自该簇采样块出现过的标题：w0000 留下 T0；w0001 簇内无 T0 → 清空；T9 全局不存在 → 丢弃
    assert entries[0].see_also == ["T0"] and entries[1].see_also == []

    llm2 = EntryLLM()
    stats2 = build_wiki(chunks, vectors, out, llm2, k=2, log=lambda *a: None)
    assert stats2["built"] == 0 and stats2["skipped"] == 2 and llm2.calls == []

    with pytest.raises(ValueError, match="参数不一致"):
        build_wiki(chunks, vectors, out, EntryLLM(), k=3, log=lambda *a: None)


def test_generate_entry_escalates_budget_after_empty_output():
    chunks = _chunks(3)

    class LadderLLM(EntryLLM):
        def __init__(self):
            super().__init__({"title": "T", "summary": "S", "see_also": []})
            self.script = ["", "", json.dumps(self.payload)]

        def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
            self.calls.append({"tag": tag, "max_tokens": max_tokens})
            text = self.script.pop(0) if self.script else json.dumps(self.payload)
            return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                               total_tokens=2, latency_ms=1.0,
                               finish_reason="stop" if text else "length")

    llm = LadderLLM()
    entry = generate_entry(llm, chunks, [0, 1], log=lambda *a: None)
    assert entry is not None and entry["title"] == "T"
    # 首轮无提示 2048 → 带收尾提示 2048 → 带收尾提示 4096（末级预算必须更大）
    assert [c["max_tokens"] for c in llm.calls] == [2048, 2048, 4096]
    assert [c["tag"] for c in llm.calls] == ["wiki_entry", "wiki_entry_retry", "wiki_entry_retry"]


def test_build_wiki_skips_unparsable_entries(tmp_path):
    chunks = _chunks(4)

    class BadLLM(EntryLLM):
        def chat(self, *a, **kw):
            self.calls.append({"tag": kw.get("tag")})
            return LLMResponse(text="", model="fake", prompt_tokens=1, completion_tokens=1,
                               total_tokens=2, latency_ms=1.0, finish_reason="length")

    stats = build_wiki(chunks, _vectors_two_groups(4), tmp_path / "w", BadLLM(), k=2, log=lambda *a: None)
    assert stats["built"] == 0 and stats["failed"] == 2 and stats["failed_ids"] == ["w0000", "w0001"]


def test_build_wiki_falls_back_to_reduced_passages(tmp_path):
    """阶梯全败后缩到 8 块重试：输入变小、推理负担变轻。"""
    chunks = _chunks(20)

    class ShrinkLLM(EntryLLM):
        def __init__(self):
            super().__init__({"title": "T", "summary": "S", "see_also": []})
            self.script = ["", "", "", json.dumps(self.payload)]  # 前三轮（阶梯）全空，缩块后成功

        def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
            self.calls.append({"tag": tag, "messages": messages})
            text = self.script.pop(0) if self.script else ""
            return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                               total_tokens=2, latency_ms=1.0,
                               finish_reason="stop" if text else "length")

    llm = ShrinkLLM()
    stats = build_wiki(chunks, _vectors_two_groups(20), tmp_path / "w", llm, k=1,
                       chunks_per_entry=16, log=lambda *a: None)
    assert stats["built"] == 1 and stats["failed"] == 0
    assert len(llm.calls) == 4
    full_prompt = llm.calls[0]["messages"][-1]["content"]
    reduced_prompt = llm.calls[3]["messages"][-1]["content"]
    assert reduced_prompt.count("[") < full_prompt.count("[")  # 段落数变少


def test_build_entry_vectors_and_related():
    entries = [WikiEntry(entry_id="w0", title="A", summary="s", chunk_ids=["c0"]),
               WikiEntry(entry_id="w1", title="B", summary="s", chunk_ids=["c1"]),
               WikiEntry(entry_id="w2", title="C", summary="s", chunk_ids=["c2"])]
    emb = DictEmbedder({"A\ns": [1, 0], "B\ns": [0.9, 0.1], "C\ns": [0, 1]})
    vecs = build_entry_vectors(entries, emb)
    assert vecs.shape == (3, 2)
    assert compute_related(vecs, top_n=1) == [[1], [0], [1]]


def _dense_index():
    return DenseIndex(["c0", "c1", "c2"],
                      np.array([[1.0, 0.0], [0.8, 0.6], [0.0, 1.0]], dtype=np.float32),
                      embedder=DictEmbedder({"q": [1.0, 0.0]}))


def _wiki_index(see_also, related):
    entries = [WikiEntry(entry_id="w0", title="Alpha topic", summary="s",
                         chunk_ids=["c0", "c1"], see_also=see_also),
               WikiEntry(entry_id="w1", title="Gamma topic", summary="s",
                         chunk_ids=["c2"], see_also=[])]
    entry_vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    wiki = WikiIndex(entries, entry_vectors, _dense_index(), {"Gamma": ["c2"]},
                     n_entries_hit=1, per_entry=3, per_link=2)
    wiki.related = related  # 覆盖结构近邻，隔离被测路径
    return wiki


def test_wiki_search_sources_plus_see_also_expansion():
    wiki = _wiki_index(see_also=["Gamma"], related=[[], []])
    hits = wiki.search("q", k=10)
    assert [cid for cid, _ in hits] == ["c0", "c1", "c2"]  # c2 只能来自 see-also 标题跳转
    assert hits[0][1] == pytest.approx(1.0)


def test_wiki_search_related_entry_expansion_without_see_also():
    wiki = _wiki_index(see_also=[], related=[[1], [0]])
    hits = wiki.search("q", k=10)
    assert [cid for cid, _ in hits] == ["c0", "c1", "c2"]  # c2 来自结构近邻条目
    assert wiki.search("q", k=2) == [("c0", 1.0), ("c1", pytest.approx(0.8))]


def test_wiki_index_load_requires_built_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="entries.jsonl"):
        WikiIndex.load(tmp_path, dense=_dense_index(), chunks=_chunks(3))


def test_wiki_prior_scores_covers_members_and_links():
    wiki = _wiki_index(see_also=["Gamma"], related=[[], []])
    prior = wiki.prior_scores("q", top_e=1)
    # top1 条目 w0 的成员 c0/c1 与其 see-also "Gamma" 的块 c2 都拿先验分 1/(60+1)
    assert set(prior) == {"c0", "c1", "c2"}
    assert prior["c0"] == pytest.approx(1.0 / 61) == prior["c2"]


def test_wiki_pullback_orders_by_query_cosine():
    wiki = _wiki_index(see_also=["Gamma"], related=[[], []])
    # 查询向量 [1,0]：c0 余弦 1.0、c1 0.8、c2 0.0
    out = wiki.pullback("q", top_e=1, per_entry=2, per_link=1)
    assert out == ["c0", "c1", "c2"]
