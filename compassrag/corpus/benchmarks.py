"""三基准（HotpotQA / 2WikiMultiHopQA / MuSiQue）dev 集下载与归一化。

归一化后的统一记录：
  id: str            原始题 id
  question: str
  answer: str
  type: str|None     题型（hotpotqa: comparison/bridge…；2wiki: comparison/compositional/bridge_comparison/inference）
  stratum: str       分层键（hotpotqa: level；2wiki: 题型 type；musique: 跳数）
  paragraphs: [{title, text, is_supporting}]
"""

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

from huggingface_hub import list_repo_files

BENCHMARKS = ("hotpotqa", "2wiki", "musique")

RAW_DIR_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "raw"

_UA = {"User-Agent": "CompassRAG/0.1 (research; +https://github.com/dundundun0116/compassrag)"}

# 每个基准的候选来源，依次尝试：
# direct = 直接 URL；hf = HF 数据集仓 JSON/JSONL + 文件名匹配；hf_parquet = HF 仓 parquet
_SOURCES = {
    "hotpotqa": [
        ("direct", "https://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json"),
        ("hf_parquet", "hotpotqa/hotpot_qa", r"distractor/validation-.*\.parquet$"),
    ],
    "musique": [
        ("hf", "dgslibisey/MuSiQue", r"musique_(ans_)?v1\.0_dev\.jsonl$"),
    ],
    "2wiki": [
        ("hf_parquet", "framolfese/2WikiMultihopQA", r"(^|/)validation-.*\.parquet$"),
        ("hf_parquet", "xanhho/2WikiMultihopQA", r"(^|/)dev\.parquet$"),
    ],
}


# ---------- 归一化 ----------

def _supporting_titles_dict(sf: dict) -> list[str]:
    return list(sf.get("title", []))


def _supporting_titles_list(sf: list) -> list[str]:
    return [pair[0] for pair in sf]


def normalize_hotpotqa(item: dict) -> dict:
    sf = item.get("supporting_facts", {})
    titles = set(_supporting_titles_dict(sf) if isinstance(sf, dict) else _supporting_titles_list(sf))
    return {
        "id": item["_id"],
        "question": item["question"],
        "answer": item["answer"],
        "type": item.get("type"),
        "stratum": item.get("level", "unknown"),
        "paragraphs": [
            {"title": t, "text": " ".join(sents), "is_supporting": t in titles}
            for t, sents in item["context"]
        ],
    }


def normalize_2wiki(item: dict) -> dict:
    sf = item.get("supporting_facts", {})
    titles = set(_supporting_titles_dict(sf) if isinstance(sf, dict) else _supporting_titles_list(sf))
    return {
        "id": item["_id"],
        "question": item["question"],
        "answer": item["answer"],
        "type": item.get("type"),
        "stratum": item.get("type") or str(len(titles)),
        "paragraphs": [
            {"title": t, "text": " ".join(sents), "is_supporting": t in titles}
            for t, sents in item["context"]
        ],
    }


def normalize_musique(item: dict) -> dict:
    paras = item["paragraphs"]
    return {
        "id": item["id"],
        "question": item["question"],
        "answer": item["answer"],
        "stratum": str(sum(1 for p in paras if p["is_supporting"])),
        "paragraphs": [
            {"title": p["title"], "text": p["paragraph_text"], "is_supporting": bool(p["is_supporting"])}
            for p in paras
        ],
    }


_NORMALIZERS = {"hotpotqa": normalize_hotpotqa, "2wiki": normalize_2wiki, "musique": normalize_musique}


def adapt_parquet_row(bench: str, row: dict) -> dict:
    """HF parquet 行（struct-of-lists）→ 旧版 JSON 条目形状，供 normalize_* 消费。"""
    ctx = row["context"]
    sf = row["supporting_facts"]
    item = {
        "_id": row["id"],
        "question": row["question"],
        "answer": row["answer"],
        "type": row.get("type"),
        "context": list(zip(ctx["title"], ctx["sentences"])),
        "supporting_facts": {"title": list(sf["title"]), "sent_id": list(sf["sent_id"])},
    }
    if bench == "hotpotqa":
        item["level"] = row.get("level")
    return item


# ---------- 下载与加载 ----------

def _download(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers=_UA)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as f:
        total = 0
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            total += len(block)
            f.write(block)
    if total < 1024:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载内容过小（{total}B），疑似错误页：{url}")
    tmp.replace(dest)
    return dest


def _hf_candidate_urls(repo: str, pattern: str) -> list[str]:
    files = list_repo_files(repo, repo_type="dataset")
    rx = re.compile(pattern)
    matches = [f for f in files if rx.search(f)]
    if not matches:
        raise ValueError(f"{repo} 中无匹配 {pattern!r} 的文件（全部：{files[:20]}）")
    # 多个匹配时取最浅层级、文件名最小者，保证确定性
    matches.sort(key=lambda p: (p.count("/"), p))
    endpoint = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    return [f"{endpoint}/datasets/{repo}/resolve/main/{m}" for m in matches]


def _is_parsable(path: Path) -> bool:
    """缓存/下载产物必须是可完整解析的数据文件，防止错误页毒化缓存。"""
    try:
        _load_items(path)
        return True
    except Exception:  # noqa: BLE001 任何解析失败都视为无效产物
        return False


def download_dev(bench: str, raw_dir: Path = RAW_DIR_DEFAULT) -> Path:
    """下载 bench 的 dev 原始文件（带缓存），返回本地路径。"""
    if bench not in BENCHMARKS:
        raise ValueError(f"未知基准 {bench!r}，可选：{BENCHMARKS}")
    dest_dir = Path(raw_dir) / bench
    existing = sorted(p for p in dest_dir.glob("*") if p.is_file() and p.suffix != ".part") if dest_dir.exists() else []
    if existing:
        return existing[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for source in _SOURCES[bench]:
        try:
            if source[0] == "direct":
                urls = [source[1]]
            else:
                urls = _hf_candidate_urls(source[1], source[2])
            for url in urls:
                try:
                    path = _download(url, dest_dir / Path(url.split("?")[0]).name)
                    if not _is_parsable(path):
                        path.unlink(missing_ok=True)
                        raise RuntimeError("内容无法解析为数据文件（疑似错误页），已删除")
                    print(f"[{bench}] 已下载 {url} -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
                    return path
                except Exception as e:  # noqa: BLE001 逐候选降级
                    errors.append(f"{url}: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source}: {e}")
    raise RuntimeError(f"[{bench}] 所有候选来源均失败：\n" + "\n".join(errors))


def _load_items(path: Path) -> list[dict]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    return json.loads(path.read_text(encoding="utf-8"))


def load_dev(bench: str, raw_dir: Path = RAW_DIR_DEFAULT) -> list[dict]:
    """加载并归一化 dev 集，按 id 排序保证确定性。"""
    path = download_dev(bench, raw_dir)
    items = _load_items(path)
    if path.suffix == ".parquet":
        items = [adapt_parquet_row(bench, it) for it in items]
    normalize = _NORMALIZERS[bench]
    records = [normalize(it) for it in items if it.get("question") and it.get("answer")]
    records.sort(key=lambda r: r["id"])
    return records


def source_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
