import json

import numpy as np
import pytest

from compassrag.index.kg import (
    EntityGraph,
    Extraction,
    KGSearcher,
    extract_chunk,
    ner_query,
    parse_entities_json,
    parse_extraction_json,
)
from compassrag.llm.client import LLMResponse


def test_parse_extraction_json_tolerates_fences_and_noise():
    good = json.dumps({"entities": ["Alice"], "triples": [["Alice", "spouse", "Bob"]]})
    assert parse_extraction_json(good)["entities"] == ["Alice"]
    fenced = "```json\n" + good + "\n```"
    assert parse_extraction_json(fenced)["triples"] == [["Alice", "spouse", "Bob"]]
    noisy = "Here you go: " + good + " hope it helps"
    assert parse_extraction_json(noisy)["entities"] == ["Alice"]
    assert parse_extraction_json("") is None
    assert parse_extraction_json("not json") is None
    assert parse_extraction_json('{"entities": "Alice", "triples": []}') is None  # 类型不对
    # 非字符串实体/残缺三元组被剔除，但整体不判 None
    messy = json.dumps({"entities": ["A", 3, " ", "B"],
                        "triples": [["A", "r", "B"], ["A", "r"], "junk"]})
    out = parse_extraction_json(messy)
    assert out["entities"] == ["A", "B"] and out["triples"] == [["A", "r", "B"]]


def _ex(cid, entities, triples):
    return Extraction(chunk_id=cid, entities=entities, triples=triples)


def test_entity_graph_dedups_nodes_and_counts_passages():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["Alice", "Bob"], [["Alice", "spouse", "Bob"]]))
    g.add_extraction(_ex("c1", ["Bob", "Carol"], [["Bob", "mentor", "Carol"]]))
    # 节点按精确字符串去重：Bob 跨块共享（多跳桥）
    assert g.nodes == ["Alice", "Bob", "Carol"]
    assert len(g._edges) == 2                       # 无向去重后 2 条
    assert g.p_nodes[0] == [(0, 2), (1, 2)]         # c0：Alice(实体+三元组头)=2, Bob(实体+尾)=2
    assert (0, 1) in g._edges and (1, 2) in g._edges


def test_entity_graph_skips_self_loops_and_blank_nodes():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["", "  ", "Alice"], [["Alice", "self", "Alice"], ["", "r", "Bob"]]))
    assert g.nodes == ["Alice", "Bob"]
    assert g._edges == {}                           # 自环与空节点三元组全部丢弃


class ListEmbedder:
    """按给定向量表编码（顺序对应 nodes 插入序），供 finalize 同义边测试。"""

    def __init__(self, table):
        self.table = [np.asarray(v, dtype=np.float32) for v in table]
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return np.vstack([self.table[i] for i in range(len(texts))])


def test_finalize_adds_symmetric_synonymy_edges():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["Barack Obama", "Michelle"], [["Barack Obama", "spouse", "Michelle"]]))
    g.add_extraction(_ex("c1", ["Obama", "Hawaii"], [["Obama", "born in", "Hawaii"]]))
    v = np.array([[1.0, 0.0], [0.0, 1.0], [0.99, 0.141], [0.0, -1.0]], dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    g.finalize(ListEmbedder(v), syn_threshold=0.9, syn_topk=1)
    # "Obama"（id 2）与 "Barack Obama"（id 0）余弦 ~0.99 → 同义边；Michelle-Hawaii 反向无
    assert (0, 2) in g.syn_edges
    assert all(a != b for a, b in g.syn_edges)


def test_ppr_flows_along_edges_and_keeps_mass():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["A", "B"], [["A", "r", "B"]]))
    g.add_extraction(_ex("c1", ["B", "C"], [["B", "r", "C"]]))
    p = g.ppr([0], damping=0.5)
    assert p.sum() == pytest.approx(1.0, abs=1e-5)   # 含重启的概率守恒
    assert p[0] > 0 and p[1] > p[2] > 0              # 离种子越远概率越低
    assert g.ppr([], damping=0.5).sum() == 0.0       # 空种子 → 全零


def test_match_seeds_entity_argmax_and_fallback():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["Alice", "Bob"], [["Alice", "r", "Bob"]]))
    g.node_vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    class OneEmb:
        def encode(self, texts):
            return np.tile(np.array([1.0, 0.0], dtype=np.float32), (len(texts), 1))

    # 实体模式：每个实体 argmax 到 Alice
    assert g.match_seeds(np.array([0.0, 1.0], dtype=np.float32),
                         entities=["x", "y"], embedder=OneEmb()) == [0]
    # 回退模式：无实体 → 按查询向量取 top-k 节点
    assert g.match_seeds(np.array([0.0, 1.0], dtype=np.float32),
                         entities=[], top_fallback=1) == [1]


def test_rank_passages_aggregates_and_orders():
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["A", "B"], []))
    g.add_extraction(_ex("c1", ["A"], []))
    scores = np.array([1.0, 2.0], dtype=np.float32)   # A=1, B=2
    ranked = g.rank_passages(scores, k=2)
    # c0 含 A、B：1*1 + 2*1 = 3 > c1 的 1
    assert ranked[0][0] == "c0" and ranked[0][1] == pytest.approx(3.0)
    assert ranked[1][0] == "c1"


def test_save_load_roundtrip(tmp_path):
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["Alice", "Bob"], [["Alice", "spouse", "Bob"]]))
    g.finalize(ListEmbedder([np.array([1.0, 0.0]), np.array([0.0, 1.0])]), syn_threshold=0.99)
    g.save(tmp_path / "kg")
    g2 = EntityGraph.load(tmp_path / "kg")
    assert g2.nodes == g.nodes and g2.passages == g.passages
    assert g2._edges == g._edges and g2.p_nodes == g.p_nodes
    assert g2.node_vectors is not None and g2.node_vectors.shape == (2, 2)
    assert g2.ppr([0]).shape == (2,)


class LadderLLM:
    """固定返回抽取结果的假 LLM（前几轮返回空，验证预算阶梯与收尾提示）。"""

    def __init__(self):
        self.payload = json.dumps({"entities": ["Alice"], "triples": [["Alice", "r", "Bob"]]})
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None,
             thinking=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens,
                           "nudged": "Output the JSON now" in messages[-1]["content"],
                           "thinking": thinking})
        text = self.payload if len(self.calls) >= 2 else ""
        return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                           total_tokens=2, latency_ms=1.0,
                           finish_reason="stop" if text else "length")


def test_extract_chunk_escalates_budget_after_empty_output():
    llm = LadderLLM()
    ex = extract_chunk(llm, "Alice met Bob.", log=lambda *a: None)
    assert ex is not None and ex.entities == ["Alice"]
    assert [c["max_tokens"] for c in llm.calls] == [1024, 2048]
    assert [c["nudged"] for c in llm.calls] == [False, True]
    assert all(c["thinking"] is False for c in llm.calls)  # 机械任务必须关思维链


def test_parse_entities_json():
    assert parse_entities_json('{"entities": ["Stanford", "Alzheimer"]}') == ["Stanford", "Alzheimer"]
    assert parse_entities_json("```json\n{\"entities\": []}\n```") == []
    assert parse_entities_json('{"entities": ["A", "", 3]}') == ["A"]
    assert parse_entities_json("") is None
    assert parse_entities_json('{"entities": "x"}') is None
    assert parse_entities_json("no json") is None


class NerLLM:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None,
             thinking=None):
        self.calls.append({"tag": tag, "max_tokens": max_tokens, "thinking": thinking})
        text = json.dumps({"entities": ["Sikyona"]}) if max_tokens == 1024 else ""
        return LLMResponse(text=text, model="fake", prompt_tokens=1, completion_tokens=1,
                           total_tokens=2, latency_ms=1.0,
                           finish_reason="stop" if text else "length")


def test_ner_query_escalates_and_disables_thinking():
    llm = NerLLM()
    assert ner_query(llm, "Who is Sikyona named after?") == ["Sikyona"]
    assert [c["max_tokens"] for c in llm.calls] == [512, 1024]
    assert all(c["thinking"] is False for c in llm.calls)


class QueryEmb:
    """查询嵌入 = [1,0]（命中节点 Alice）；实体嵌入 = 每词表 [0,1]（命中 Bob）。"""

    def encode(self, texts):
        if len(texts) == 1:
            return np.array([[1.0, 0.0]], dtype=np.float32)
        return np.tile(np.array([0.0, 1.0], dtype=np.float32), (len(texts), 1))


def test_kg_searcher_tool_interface_and_ner_fallback(tmp_path):
    # 两节点非对称图：Alice—Bob 三元组边；c0 含两节点、c1 只含 Bob
    g = EntityGraph()
    g.add_extraction(_ex("c0", ["Alice", "Bob"], [["Alice", "r", "Bob"]]))
    g.add_extraction(_ex("c1", ["Bob"], []))
    g.finalize(ListEmbedder([np.array([1.0, 0.0]), np.array([0.0, 1.0])]), syn_threshold=0.99)
    g.save(tmp_path / "kg")
    ner_path = tmp_path / "kg" / "query_entities.jsonl"
    ner_path.write_text(json.dumps({"question": "关于 Alice？", "entities": ["Bob"]}) + "\n",
                        encoding="utf-8")

    searcher = KGSearcher.load(tmp_path / "kg", embedder=QueryEmb())
    # NER 命中：实体 "Bob" → 种子节点 Bob → c0 与 c1 都含 Bob，均得分
    hits = searcher.search("关于 Alice？", k=10)
    assert {cid for cid, _ in hits} == {"c0", "c1"}
    # 无 NER 缓存（如改写变体查询）→ 回退整查询选种：查询向量 [1,0] 命中 Alice（Bob 0 分也入种）
    # PPR 收敛后 Alice=Bob=0.5 → c0 得分 2.0 > c1 的 1.0，确定序
    hits2 = searcher.search("改写后的变体查询", k=10)
    assert hits2[0][0] == "c0"
    # k 截断生效
    assert len(searcher.search("关于 Alice？", k=1)) == 1
