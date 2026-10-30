"""wiki 式分层摘要索引：条目层（主题摘要）+ 源块 + see-also 链接。

构建（scripts/build_wiki.py）：k-means 聚类 → 每簇采样代表块 → LLM 生成条目
（标题 / 摘要 / see-also）→ 链接清洗（see_also 只能取自簇内出现过的文章标题）。
逐簇增量落盘、断点续跑；meta 记录 k/seed，参数不符即拒绝续跑。

检索（三路 + 兜底）：查询 → 条目层（条目向量余弦 top-N）→ 源块捞回（条目成员内
余弦 top）→ see-also 链接扩展（链接标题的块 + 结构近邻条目）；块层兜底由
agent 的 BM25/dense 路由（RRF 融合）承担。
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..retrieval.fusion import RRF_K_DEFAULT
from .cluster import (
    CHUNKS_PER_ENTRY_DEFAULT,
    KMEANS_SEED_DEFAULT,
    default_k,
    kmeans_labels,
    sample_representatives,
)

ENTRY_TEXT_MAX_CHARS = 320
SEE_ALSO_MAX = 4
RELATED_ENTRIES_MAX = 3

ENTRY_PROMPT = """You are building a wiki-style index over a document collection.

Below are source passages, each with its article title. Write ONE wiki entry for this topic cluster.

Output JSON only, no other text:
{{"title": "<concise topic title>", "summary": "<2-4 sentences covering the cluster's shared topic>", "see_also": ["<article title>", "..."]}}

Rules:
- "summary": at most 3 sentences (≤70 words), covering the cluster's shared topic.
- "see_also": 2-4 related article titles, chosen ONLY from the titles listed above, exact strings.
- The summary must be grounded in the passages; do not invent facts.

Passages:
{passages}"""

ENTRY_NUDGE = "Output the JSON now. Do not analyze further."


@dataclass
class WikiEntry:
    entry_id: str
    title: str
    summary: str
    chunk_ids: list[str]
    see_also: list[str] = field(default_factory=list)
    # 结构近邻条目（构建后按条目向量余弦计算，检索期扩展用）
    related: list[str] = field(default_factory=list)


def parse_entry_json(text: str) -> dict | None:
    """容忍代码围栏与前后杂讯的 JSON 解析；缺字段或类型不对返回 None。"""
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
    title, summary = obj.get("title"), obj.get("summary")
    see_also = obj.get("see_also", [])
    if not (isinstance(title, str) and title.strip() and isinstance(summary, str) and summary.strip()):
        return None
    if not isinstance(see_also, list):
        see_also = []
    return {"title": title.strip(), "summary": summary.strip(),
            "see_also": [s for s in see_also if isinstance(s, str)]}


def clean_see_also(candidates: list[str], allowed_titles: list[str]) -> list[str]:
    """链接清洗：只保留簇内出现过的标题（大小写不敏感匹配），去重截断。"""
    by_lower = {}
    for t in allowed_titles:
        by_lower.setdefault(t.lower(), t)
    out: list[str] = []
    for c in candidates:
        t = by_lower.get(c.strip().lower())
        if t and t not in out:
            out.append(t)
        if len(out) >= SEE_ALSO_MAX:
            break
    return out


def _format_passages(chunks: list[dict], rows: list[int]) -> str:
    lines = []
    for n, i in enumerate(rows, start=1):
        c = chunks[i]
        text = " ".join(c["text"].split())[:ENTRY_TEXT_MAX_CHARS]
        lines.append(f"[{n}] {c['title']}: {text}")
    return "\n".join(lines)


def generate_entry(llm, chunks: list[dict], rows: list[int], *, budgets: tuple = (2048, 4096),
                   tag: str = "wiki_entry", log=None) -> dict | None:
    """条目生成（LLM）：首轮 budgets[0]；空输出/解析失败时带收尾提示按 budgets 逐级加预算重试。

    思维链模型在摘要任务上推理链也长（实测首轮 1024 全撞上限），故与答案协议同款阶梯。
    """
    allowed = list(dict.fromkeys(chunks[i]["title"] for i in rows))
    messages = [{"role": "system", "content": "You build compact wiki-style topic entries."},
                {"role": "user", "content": ENTRY_PROMPT.format(passages=_format_passages(chunks, rows))}]
    attempts = [(budgets[0], False)] + [(b, True) for b in budgets]
    for n, (budget, nudge) in enumerate(attempts, start=1):
        msgs = messages + ([{"role": "user", "content": ENTRY_NUDGE}] if nudge else [])
        resp = llm.chat(msgs, tag=tag if n == 1 else f"{tag}_retry", max_tokens=int(budget))
        parsed = parse_entry_json(resp.text)
        if parsed is not None:
            parsed["see_also"] = clean_see_also(parsed["see_also"], allowed)
            return parsed
        if log is not None:
            log(f"  尝试 {n}（{budget} tokens）未产出可解析条目：finish={resp.finish_reason} "
                f"输出片段={resp.text[:100]!r}")
    return None


def load_entries(path: Path) -> list[WikiEntry]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(WikiEntry(**json.loads(line)))
    return out


def build_wiki(chunks: list[dict], vectors: np.ndarray, out_dir: Path, llm, *,
               k: int | None = None, chunks_per_entry: int = CHUNKS_PER_ENTRY_DEFAULT,
               seed: int = KMEANS_SEED_DEFAULT, limit: int | None = None, log=print) -> dict:
    """聚类 + 逐簇生成条目并增量落盘（entries.jsonl）；断点续跑按 entry_id 跳过。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries_path = out_dir / "entries.jsonl"
    k = default_k(len(chunks)) if k is None else int(k)

    meta_path = out_dir / "meta.json"
    meta = {"k": k, "seed": seed, "n_chunks": len(chunks),
            "chunks_per_entry": chunks_per_entry, "entry_text_max_chars": ENTRY_TEXT_MAX_CHARS}
    if meta_path.exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        for key in ("k", "seed", "n_chunks", "chunks_per_entry"):
            if old.get(key) != meta.get(key):
                raise ValueError(f"已有索引参数不一致（{key}: {old.get(key)} != {meta.get(key)}），先清理目录再重建")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    labels_path = out_dir / "labels.npy"
    if labels_path.exists():
        labels = np.load(labels_path)
        assert labels.shape[0] == len(chunks), f"缓存簇标签行数不符：{labels.shape[0]} != {len(chunks)}"
        log(f"复用缓存簇标签（{k} 簇，改 k/seed 时请清理目录）")
    else:
        labels = kmeans_labels(vectors, k, seed=seed)
        np.save(labels_path, labels)
    done = {e.entry_id for e in load_entries(entries_path)}
    stats = {"k": k, "skipped": 0, "built": 0, "failed": 0, "failed_ids": []}
    n_target = k if limit is None else min(k, int(limit))
    with entries_path.open("a", encoding="utf-8") as f:
        for c in range(n_target):
            eid = f"w{c:04d}"
            if eid in done:
                stats["skipped"] += 1
                continue
            members = np.where(labels == c)[0]
            if members.size == 0:
                continue
            rows = sample_representatives(vectors, members, chunks_per_entry)
            parsed = generate_entry(llm, chunks, rows, log=log)
            if parsed is None and len(rows) > 8:
                # 阶梯全败多为"输入太大导致推理打转"：缩小到核心 8 块（最靠质心的）再试一次
                log(f"[{eid}] 阶梯全败，改用前 8 个代表块重试")
                parsed = generate_entry(llm, chunks, rows[:8], log=log)
            if parsed is None:
                stats["failed"] += 1
                stats["failed_ids"].append(eid)
                log(f"[{eid}] 生成失败（空输出/解析失败），跳过（可续跑补）")
                continue
            entry = WikiEntry(entry_id=eid, title=parsed["title"], summary=parsed["summary"],
                              chunk_ids=[chunks[i]["chunk_id"] for i in members],
                              see_also=parsed["see_also"])
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            f.flush()
            stats["built"] += 1
            log(f"[{eid}] {entry.title} | 源块 {len(entry.chunk_ids)} | see_also {entry.see_also}")
    return stats


def build_entry_vectors(entries: list[WikiEntry], embedder) -> np.ndarray:
    """条目向量 = 标题+摘要的 BGE-M3 编码（本地、免费），供条目层检索。"""
    texts = [f"{e.title}\n{e.summary}" for e in entries]
    return np.asarray(embedder.encode(texts), dtype=np.float32) if texts else np.zeros((0, 0), dtype=np.float32)


def compute_related(entry_vectors: np.ndarray, top_n: int = RELATED_ENTRIES_MAX) -> list[list[int]]:
    """结构 see-also：每个条目取条目向量余弦最近的 top_n 个其他条目（行号）。"""
    if entry_vectors.shape[0] <= 1:
        return [[] for _ in range(entry_vectors.shape[0])]
    sims = entry_vectors @ entry_vectors.T
    np.fill_diagonal(sims, -np.inf)
    order = np.argsort(-sims, axis=1, kind="stable")[:, :top_n]
    return [[int(j) for j in row] for row in order]


class WikiIndex:
    """三路检索：条目层 → 源块捞回 → see-also 链接扩展；返回块级排名（供 RRF 融合）。"""

    def __init__(self, entries: list[WikiEntry], entry_vectors: np.ndarray, dense,
                 title_to_ids: dict[str, list[str]], *, n_entries_hit: int = 2,
                 per_entry: int = 3, per_link: int = 2):
        assert len(entries) == entry_vectors.shape[0], \
            f"条目数 {len(entries)} != 条目向量行数 {entry_vectors.shape[0]}"
        self.entries = entries
        self.entry_vectors = entry_vectors
        self.dense = dense
        self.title_to_ids = title_to_ids
        self.related = compute_related(entry_vectors)
        self.n_entries_hit = n_entries_hit
        self.per_entry = per_entry
        self.per_link = per_link

    @classmethod
    def load(cls, dir_path: Path, dense, chunks: list[dict]) -> "WikiIndex":
        dir_path = Path(dir_path)
        entries = load_entries(dir_path / "entries.jsonl")
        if not entries:
            raise FileNotFoundError(f"{dir_path}/entries.jsonl 不存在或为空——先跑 scripts/build_wiki.py")
        vec_path = dir_path / "entry_vectors.npy"
        if not vec_path.exists():
            raise FileNotFoundError(f"缺少 {vec_path}——构建脚本收尾会生成（对已有条目重算）")
        vectors = np.load(vec_path)
        title_to_ids: dict[str, list[str]] = {}
        for c in chunks:
            title_to_ids.setdefault(c["title"], []).append(c["chunk_id"])
        return cls(entries, vectors, dense, title_to_ids)

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        q = self.dense.encode_query(query)
        e_scores = self.entry_vectors @ q
        n_hit = min(self.n_entries_hit, len(self.entries))
        top_e = np.argsort(-e_scores, kind="stable")[:n_hit]

        cand: dict[str, float] = {}

        def add(hits):
            for cid, s in hits:
                if s > cand.get(cid, -np.inf):
                    cand[cid] = s

        for ei in top_e:
            e = self.entries[ei]
            add(self.dense.top_within(q, e.chunk_ids, k=self.per_entry))          # 源块捞回
            link_ids = [cid for t in e.see_also for cid in self.title_to_ids.get(t, [])]
            add(self.dense.top_within(q, link_ids, k=self.per_link))              # 链接标题跳转
            for rj in self.related[int(ei)][:2]:                                  # 结构近邻条目
                add(self.dense.top_within(q, self.entries[rj].chunk_ids, k=self.per_link))

        ordered = sorted(cand.items(), key=lambda kv: -kv[1])
        return ordered[:k]

    def prior_scores(self, query: str, top_e: int = 3) -> dict[str, float]:
        """条目层先验（修订二：wiki 只当重排信号，不当竞争路）。

        查询 → 条目余弦 top_e 条目；命中条目的成员块与 see-also 链接标题的块
        获得 RRF 同款先验分 1/(K+条目名次)，块取其所属条目中的最高（最小）名次分。
        用法：对混合检索候选 score += boost · prior[cid] 后重排——先验只调座次，
        不送新候选（补捞见 pullback）。
        """
        q = self.dense.encode_query(query)
        e_scores = self.entry_vectors @ q
        n_hit = min(top_e, len(self.entries))
        top = np.argsort(-e_scores, kind="stable")[:n_hit]
        prior: dict[str, float] = {}
        for rank, ei in enumerate(top, start=1):
            s = 1.0 / (RRF_K_DEFAULT + rank)
            for cid in self.entries[int(ei)].chunk_ids:
                if s > prior.get(cid, 0.0):
                    prior[cid] = s
            for t in self.entries[int(ei)].see_also:
                for cid in self.title_to_ids.get(t, []):
                    if s > prior.get(cid, 0.0):
                        prior[cid] = s
        return prior

    def pullback(self, query: str, top_e: int = 3, per_entry: int = 3, per_link: int = 2) -> list[str]:
        """条目内补捞：top_e 条目的成员块 + see-also 链接块中按查询余弦取头部（有序 id）。

        供先验重排的可选候选扩充——只从命中条目内部捞，不再展开近邻条目（区别于 search）。
        """
        q = self.dense.encode_query(query)
        e_scores = self.entry_vectors @ q
        n_hit = min(top_e, len(self.entries))
        top = np.argsort(-e_scores, kind="stable")[:n_hit]
        cand: dict[str, float] = {}
        for ei in top:
            e = self.entries[int(ei)]
            for cid, s in self.dense.top_within(q, e.chunk_ids, k=per_entry):
                cand[cid] = max(cand.get(cid, -np.inf), s)
            link_ids = [cid for t in e.see_also for cid in self.title_to_ids.get(t, [])]
            for cid, s in self.dense.top_within(q, link_ids, k=per_link):
                cand[cid] = max(cand.get(cid, -np.inf), s)
        return [cid for cid, _ in sorted(cand.items(), key=lambda kv: -kv[1])]
