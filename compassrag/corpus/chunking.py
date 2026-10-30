"""分块：段落为天然单元，超长段落按句子边界切窗（带字符重叠）。

chunk_id = sha1(title + 0x00 + 子块文本)[:16]——同题共享段落天然去重。
"""

import hashlib
import re

MAX_CHARS_DEFAULT = 1800  # ≈ 450 token
OVERLAP_DEFAULT = 200

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def chunk_id(title: str, text: str) -> str:
    return hashlib.sha1(f"{title}\x00{text}".encode("utf-8")).hexdigest()[:16]


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    return [sentence[i:i + max_chars] for i in range(0, len(sentence), max_chars)]


def _tail_fit(sents: list[str], overlap: int) -> list[str]:
    out: list[str] = []
    total = 0
    for s in reversed(sents):
        if total + len(s) + 1 > overlap:
            break
        out.insert(0, s)
        total += len(s) + 1
    return out


def chunk_text(text: str, max_chars: int = MAX_CHARS_DEFAULT,
               overlap: int = OVERLAP_DEFAULT) -> list[str]:
    text = normalize_ws(text)
    if len(text) <= max_chars:
        return [text] if text else []

    sentences: list[str] = []
    for s in _SENT_SPLIT.split(text):
        if len(s) > max_chars:
            sentences.extend(_hard_split(s, max_chars))
        else:
            sentences.append(s)

    windows: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        if cur and cur_len + len(s) + 1 > max_chars:
            windows.append(" ".join(cur))
            cur = _tail_fit(cur, overlap)
            cur_len = sum(len(x) + 1 for x in cur)
        cur.append(s)
        cur_len += len(s) + 1
    if cur:
        windows.append(" ".join(cur))
    return windows


def iter_chunks(records: list[dict], benchmark: str, *,
                max_chars: int = MAX_CHARS_DEFAULT,
                overlap: int = OVERLAP_DEFAULT):
    """遍历归一化记录的段落 → 唯一块字典流（同 (title, text) 只出一次）。

    块上不落 is_supporting：同一段落可对甲题是支撑、对乙题是干扰，
    证据归属由评测期按各题的支撑标题 join 决定；question_id 仅作首见溯源。
    """
    seen: set[str] = set()
    for r in records:
        for p in r["paragraphs"]:
            for sub in chunk_text(p["text"], max_chars, overlap):
                cid = chunk_id(p["title"], sub)
                if cid in seen:
                    continue
                seen.add(cid)
                yield {
                    "chunk_id": cid,
                    "benchmark": benchmark,
                    "question_id": r["id"],
                    "title": p["title"],
                    "text": sub,
                    "n_chars": len(sub),
                    "n_tokens_est": len(sub) // 4,
                }
