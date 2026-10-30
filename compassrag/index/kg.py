"""实体关系图索引（HippoRAG 式）：开放信息抽取 → 实体图 → Personalized PageRank。

动机（2026-10-10 调研定案，取代 wiki 摘要索引的索引层地位）：wiki 条目与 chunk 同一
编码器、同一信号空间、粒度粗百倍，实测无判别增量；多跳的"桥"是跨文档共享实体，
不是话题摘要。实体图注入 LLM 抽取的关系知识（dense 拿不到的新信号），PPR 单步扩散
即完成多跳联想；查询时零 API（本地迭代）。参照：HippoRAG（NeurIPS 2024）、
HippoRAG 2（ICML 2025）、SiReRAG（ICLR 2025，相似性/关联性二分）。

构建（scripts/build_kg.py）：每块一次 LLM 抽取（实体 + 三元组，JSON），逐块增量落盘、
断点续跑；finalize 本地完成：节点嵌入（BGE-M3）→ 同义边（余弦 ≥ 阈值）→ 图落盘。

检索：查询实体（LLM NER，scripts/ner_queries.py 缓存）→ 嵌入匹配图节点作种子 →
PPR 扩散 → 按块-节点计数矩阵聚合出块级排名（新检索信号，与 bm25/dense 融合）。
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

NODE_MAX_CHARS = 64
MAX_ENTITIES = 10
MAX_TRIPLES = 8

EXTRACTION_PROMPT = """Extract named entities and OpenIE-style fact triples from the passage.

Output JSON only, no other text:
{{"entities": ["<named entity>", "..."], "triples": [["<head>", "<relation>", "<tail>"], "..."]}}

Rules:
- "entities": the most salient named entities in the passage (people, places, organizations,
  works, events, dates), at most {max_entities}.
- "triples": facts stated in the passage, at most {max_triples}; head and tail are noun
  phrases that appear in the passage; relation is a short phrase.
- Ground everything in the passage; do not invent facts.

Passage:
{passage}"""

EXTRACTION_NUDGE = "Output the JSON now. Do not analyze further."

QUERY_NER_PROMPT = """Extract the named entities mentioned in the question.

Output JSON only, no other text:
{{"entities": ["<named entity>", "..."]}}

Rules:
- At most 8 entities; keep original capitalization.
- Include people, places, organizations, works, events and dates the question is about.
- Do not invent entities that are not in the question.

Question: {question}"""


def parse_extraction_json(text: str) -> dict | None:
    """容忍代码围栏与前后杂讯的抽取结果解析；字段类型不对返回 None。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    entities = obj.get("entities", [])
    triples = obj.get("triples", [])
    if not isinstance(entities, list) or not isinstance(triples, list):
        return None
    entities = [e.strip() for e in entities if isinstance(e, str) and e.strip()]
    clean_triples = []
    for tr in triples:
        if isinstance(tr, list) and len(tr) >= 3 and all(isinstance(x, str) and x.strip() for x in tr[:3]):
            clean_triples.append([x.strip() for x in tr[:3]])
    return {"entities": entities[:MAX_ENTITIES], "triples": clean_triples[:MAX_TRIPLES]}


def _norm_node(s: str) -> str | None:
    s = " ".join(s.split())
    if not s or len(s) > NODE_MAX_CHARS:
        return None
    return s


@dataclass
class Extraction:
    chunk_id: str
    entities: list[str]
    triples: list[list[str]]


class EntityGraph:
    """实体图：节点 = 实体名词短语；边 = 三元组（计数加权）+ 同义边；块-节点计数矩阵。

    节点按精确字符串去重（变体交给同义边）；passages 为块 id 的插入序，
    p_nodes[p] = [(node_id, count)] 供 PPR 分数聚合。
    """

    def __init__(self):
        self.nodes: list[str] = []
        self._node_id: dict[str, int] = {}
        self.passages: list[str] = []
        self._passage_row: dict[str, int] = {}
        self.p_nodes: list[list[tuple[int, int]]] = []       # 块 -> [(节点, 出现次数)]
        self._edges: dict[tuple[int, int], float] = {}       # (min,max) 节点对 -> 三元组计数
        self.syn_edges: dict[tuple[int, int], float] = {}    # finalize 填充
        self.node_vectors: np.ndarray | None = None

    # ---------- 构建 ----------

    def add_extraction(self, ex: Extraction) -> None:
        def nid(s: str) -> int | None:
            s = _norm_node(s)
            if s is None:
                return None
            if s not in self._node_id:
                self._node_id[s] = len(self.nodes)
                self.nodes.append(s)
            return self._node_id[s]

        row = self._passage_row.get(ex.chunk_id)
        if row is None:
            row = len(self.passages)
            self._passage_row[ex.chunk_id] = row
            self.passages.append(ex.chunk_id)
            self.p_nodes.append([])
        counts: dict[int, int] = {}
        for name in list(ex.entities) + [x for tr in ex.triples for x in (tr[0], tr[2])]:
            i = nid(name)
            if i is not None:
                counts[i] = counts.get(i, 0) + 1
        self.p_nodes[row] = sorted(counts.items())
        for tr in ex.triples:
            h, t = nid(tr[0]), nid(tr[2])
            if h is None or t is None or h == t:
                continue
            key = (min(h, t), max(h, t))
            self._edges[key] = self._edges.get(key, 0.0) + 1.0

    def finalize(self, embedder, *, syn_threshold: float = 0.82, syn_topk: int = 2, log=print) -> None:
        """节点嵌入（本地）+ 同义边（块状 top-k 余弦，对称去重）。"""
        self.node_vectors = np.asarray(embedder.encode(self.nodes), dtype=np.float32) \
            if self.nodes else np.zeros((0, 0), dtype=np.float32)
        n = len(self.nodes)
        if n < 2:
            return
        vecs = self.node_vectors
        block = 512
        for start in range(0, n, block):
            end = min(start + block, n)
            sims = vecs[start:end] @ vecs.T                       # (b, n)
            for r in range(end - start):
                i = start + r
                row = sims[r].copy()
                row[i] = -np.inf
                cand = np.argsort(-row, kind="stable")[:syn_topk]
                for j in cand:
                    if row[j] >= syn_threshold:
                        self.syn_edges[(min(i, int(j)), max(i, int(j)))] = float(row[j])
        log(f"同义边：{len(self.syn_edges)} 条（阈值 {syn_threshold}，top{syn_topk}）")

    # ---------- 检索 ----------

    def _edge_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        src, dst, w = [], [], []
        for (a, b), weight in self._edges.items():
            src += [a, b]; dst += [b, a]; w += [weight, weight]   # 无向：双向各一条
        for (a, b), cos in self.syn_edges.items():
            src += [a, b]; dst += [b, a]; w += [cos, cos]
        return (np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64),
                np.array(w, dtype=np.float32))

    def ppr(self, seed_ids: list[int], *, damping: float = 0.5, iters: int = 20,
            tol: float = 1e-6) -> np.ndarray:
        """Personalized PageRank：从种子节点出发的概率扩散（多跳联想单步完成）。"""
        n = len(self.nodes)
        if n == 0 or not seed_ids:
            return np.zeros(n, dtype=np.float32)
        seeds = np.array(sorted(set(seed_ids)), dtype=np.int64)
        p = np.zeros(n, dtype=np.float64)
        p[seeds] = 1.0 / len(seeds)
        src, dst, w = self._edge_arrays()
        if src.size == 0:
            return p.astype(np.float32)
        out_deg = np.zeros(n, dtype=np.float64)
        np.add.at(out_deg, src, w)
        valid = out_deg[src] > 0
        src, dst, w = src[valid], dst[valid], w[valid]
        for _ in range(iters):
            contrib = damping * p[src] * (w / out_deg[src])
            nxt = np.zeros(n, dtype=np.float64)
            np.add.at(nxt, dst, contrib)
            nxt[seeds] += (1.0 - damping) / len(seeds)
            delta = np.abs(nxt - p).sum()
            p = nxt
            if delta < tol:
                break
        return p.astype(np.float32)

    def match_seeds(self, query_embedding: np.ndarray, entities: list[str] | None = None,
                    embedder=None, *, top_fallback: int = 5, max_seeds: int = 8) -> list[int]:
        """查询实体 → 图节点（余弦 argmax）；无实体或全未命中时回退整查询 top-k 节点。"""
        if self.node_vectors is None or self.node_vectors.shape[0] == 0:
            return []
        seeds: list[int] = []
        if entities and embedder is not None:
            ev = np.asarray(embedder.encode(entities), dtype=np.float32)
            sims = ev @ self.node_vectors.T
            for row in sims:
                j = int(np.argmax(row))
                seeds.append(j)
        if not seeds:
            sims = query_embedding.reshape(1, -1) @ self.node_vectors.T
            seeds = list(np.argsort(-sims[0], kind="stable")[:top_fallback])
        return list(dict.fromkeys(seeds))[:max_seeds]

    def rank_passages(self, node_scores: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        """块级得分 = Σ 节点 PPR 分 × 块内出现次数；降序取前 k。"""
        scored: list[tuple[str, float]] = []
        for row, cid in enumerate(self.passages):
            s = sum(node_scores[nid] * cnt for nid, cnt in self.p_nodes[row])
            if s > 0:
                scored.append((cid, float(s)))
        scored.sort(key=lambda kv: -kv[1])
        return scored[:k]

    # ---------- 落盘 ----------

    def save(self, dir_path: Path) -> None:
        dir_path = Path(dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": self.nodes,
            "passages": self.passages,
            "p_nodes": self.p_nodes,
            "edges": [[a, b, w] for (a, b), w in sorted(self._edges.items())],
            "syn_edges": [[a, b, round(c, 4)] for (a, b), c in sorted(self.syn_edges.items())],
        }
        (dir_path / "graph.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if self.node_vectors is not None:
            np.save(dir_path / "node_vectors.npy", self.node_vectors)

    @classmethod
    def load(cls, dir_path: Path) -> "EntityGraph":
        dir_path = Path(dir_path)
        data = json.loads((dir_path / "graph.json").read_text(encoding="utf-8"))
        g = cls()
        g.nodes = data["nodes"]
        g._node_id = {s: i for i, s in enumerate(g.nodes)}
        g.passages = data["passages"]
        g._passage_row = {c: i for i, c in enumerate(g.passages)}
        g.p_nodes = [[(int(n), int(c)) for n, c in row] for row in data["p_nodes"]]
        g._edges = {(int(a), int(b)): float(w) for a, b, w in data["edges"]}
        g.syn_edges = {(int(a), int(b)): float(c) for a, b, c in data.get("syn_edges", [])}
        vec_path = dir_path / "node_vectors.npy"
        if vec_path.exists():
            g.node_vectors = np.load(vec_path)
        return g


def extract_chunk(llm, passage: str, *, budgets: tuple = (1024, 2048),
                  tag: str = "kg_openie", log=None) -> Extraction | None:
    """单块抽取（LLM，关闭思维链）：机械任务关链后不被推理打转拖垮（实测 reasoning 吃光预算）。

    阶梯保留作兜底：首轮 budgets[0]；空输出/解析失败带收尾提示按预算重试一次。
    """
    messages = [{"role": "system", "content": "You extract entities and fact triples from text."},
                {"role": "user", "content": EXTRACTION_PROMPT.format(
                    passage=passage, max_entities=MAX_ENTITIES, max_triples=MAX_TRIPLES)}]
    attempts = [(budgets[0], False)] + [(b, True) for b in budgets[1:]]
    for n, (budget, nudge) in enumerate(attempts, start=1):
        msgs = messages + ([{"role": "user", "content": EXTRACTION_NUDGE}] if nudge else [])
        resp = llm.chat(msgs, tag=tag if n == 1 else f"{tag}_retry", max_tokens=int(budget),
                        thinking=False)
        parsed = parse_extraction_json(resp.text)
        if parsed is not None:
            return Extraction(chunk_id="", entities=parsed["entities"],
                              triples=parsed["triples"])
        if log is not None:
            log(f"  尝试 {n}（{budget} tokens）未产出可解析抽取：finish={resp.finish_reason}")
    return None


def parse_entities_json(text: str) -> list[str] | None:
    """查询 NER 结果解析：{"entities": [...]}；解析不出返回 None（区分于合法空表）。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("entities"), list):
        return None
    out = [e.strip() for e in obj["entities"] if isinstance(e, str) and e.strip()]
    return out[:8]


def ner_query(llm, question: str, *, budgets: tuple = (512, 1024),
              tag: str = "kg_ner") -> list[str] | None:
    """查询侧实体识别（LLM，关闭思维链）；解析不出返回 None。"""
    messages = [{"role": "system", "content": "You extract named entities from questions."},
                {"role": "user", "content": QUERY_NER_PROMPT.format(question=question)}]
    attempts = [(budgets[0], False)] + [(b, True) for b in budgets[1:]]
    for n, (budget, nudge) in enumerate(attempts, start=1):
        msgs = messages + ([{"role": "user", "content": EXTRACTION_NUDGE}] if nudge else [])
        resp = llm.chat(msgs, tag=tag if n == 1 else f"{tag}_retry", max_tokens=int(budget),
                        thinking=False)
        entities = parse_entities_json(resp.text)
        if entities is not None:
            return entities
    return None


class KGSearcher:
    """实体图检索工具（agent 工具接口 search(query, k) -> [(chunk_id, score)]）。

    查询 → NER 缓存取实体（缓存按原问题文本索引；改写变体等未命中时自动回退
    整查询嵌入 top-k 选种）→ 图上种子 → PPR → 块级排名。评测脚本与 agent 共用
    同一实现，保证检索层实验与 QA 行口径一致。
    """

    def __init__(self, graph: EntityGraph, embedder,
                 ner_by_question: dict[str, list[str]] | None = None,
                 *, damping: float = 0.5):
        self.graph = graph
        self.embedder = embedder
        self.ner_by_question = ner_by_question or {}
        self.damping = damping

    @classmethod
    def load(cls, dir_path: Path, embedder, *, damping: float = 0.5) -> "KGSearcher":
        dir_path = Path(dir_path)
        graph = EntityGraph.load(dir_path)
        ner_path = dir_path / "query_entities.jsonl"
        ner: dict[str, list[str]] = {}
        if ner_path.exists():
            for line in ner_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    r = json.loads(line)
                    ner[r["question"]] = r.get("entities", [])
        return cls(graph, embedder, ner, damping=damping)

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        qv = np.asarray(self.embedder.encode([query])[0], dtype=np.float32)
        seeds = self.graph.match_seeds(qv, entities=self.ner_by_question.get(query),
                                       embedder=self.embedder)
        ppr = self.graph.ppr(seeds, damping=self.damping)
        return self.graph.rank_passages(ppr, k=k)
