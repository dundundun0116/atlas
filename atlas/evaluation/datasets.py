"""公开数据集适配：把外部 benchmark 的记录映射成内部任务（设计文档 §11 Phase 5）。

**先说清楚这一层不做什么**：它只做**格式映射与校验**，不夹带任何效果声明。
接了公开集 ≠ 跑过公开集；跑过 ≠ 结果可引用（还要样本量、冻结配置、独立判分）。
所以 `from_record` 里每个字段都要能追到来源，映射错就抛错，不静默补默认值。

**形状约束（来自我们自己的 oracle）**：`AnswerOracle` 是确定性的——只比 gold 与干扰项
（整数词边界），所以：

- gold 必须是**数值**；文本型答案要先确定口径再改 oracle，不在适配层偷偷放宽；
- 语料里 gold 只出现在一个 chunk（用 `ANSWER:<gold>` 标记），干扰项**不带**标记；
- 干扰项要显式给出来，否则"答对"可能只是碰巧抄到了第一个数字。

这三条不满足就拒绝加载，而不是"尽力而为"——评测里最贵的错误是**悄悄变松的判分**。
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from atlas.evaluation.longhorizon import Chunk, GeneratedTask

#: 我们在语料里标答案的约定（与 `generate_tasks` 一致）。
ANSWER_MARK = "ANSWER:"


class DatasetFormatError(ValueError):
    """记录不符合可判分形状——拒绝加载，不猜。"""


#: 默认字段名；不同 benchmark 叫法不一样，用 `field_map` 覆盖。
DEFAULT_FIELDS = {
    "id": "id",
    "question": "question",
    "answer": "answer",
    "contexts": "contexts",
    "distractors": "distractors",
}


def _require(record: Mapping[str, Any], key: str, source: str) -> Any:
    if key not in record or record[key] in (None, ""):
        raise DatasetFormatError(f"{source}: 缺少字段 {key!r}")
    return record[key]


def _numeric_gold(answer: Any, source: str) -> str:
    text = str(answer).strip()
    if not text.isdigit():
        raise DatasetFormatError(
            f"{source}: gold={answer!r} 不是数值。"
            "AnswerOracle 是确定性的整数比对，文本答案要先定口径再改 oracle，"
            "不在适配层放宽（那会让判分悄悄变松）。"
        )
    return text


def _chunks(raw: Any, gold: str, source: str) -> List[Chunk]:
    entries = raw if isinstance(raw, list) else [raw]
    if not entries:
        raise DatasetFormatError(f"{source}: contexts 为空，没东西可检索")
    chunks: List[Chunk] = []
    marked = 0
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            text, name = entry, f"doc-{index}"
        elif isinstance(entry, Mapping):
            text = str(entry.get("text") or entry.get("quote") or "")
            name = str(entry.get("source") or entry.get("title") or f"doc-{index}")
        else:
            raise DatasetFormatError(f"{source}: contexts[{index}] 既不是字符串也不是对象")
        if not text.strip():
            raise DatasetFormatError(f"{source}: contexts[{index}] 文本为空")
        if ANSWER_MARK in text:
            marked += 1
        chunks.append(Chunk(source=name, chunk_id=f"{source}-c{index}", quote=text))
    if marked == 0:
        raise DatasetFormatError(
            f"{source}: 语料里没有 {ANSWER_MARK}<gold> 标记，"
            "评测里「证据到底有没有到模型面前」就无法判定")
    if marked > 1:
        raise DatasetFormatError(f"{source}: 有 {marked} 个 {ANSWER_MARK} 标记，只允许一个")
    return chunks


def from_record(record: Mapping[str, Any], *,
                fields: Optional[Mapping[str, str]] = None,
                source: str = "record") -> GeneratedTask:
    """把一条外部记录映射成 `GeneratedTask`。字段名可用 `fields` 覆盖。"""
    names = {**DEFAULT_FIELDS, **dict(fields or {})}
    task_id = str(_require(record, names["id"], source))
    question = str(_require(record, names["question"], source)).strip()
    if not question:
        raise DatasetFormatError(f"{source}: question 为空")
    gold = _numeric_gold(_require(record, names["answer"], source), source)
    chunks = _chunks(_require(record, names["contexts"], source), gold, source)
    distractors = record.get(names["distractors"]) or []
    if isinstance(distractors, str):
        distractors = [distractors]
    distractors = tuple(str(item).strip() for item in distractors if str(item).strip())
    return GeneratedTask(task_id=task_id, question=question, gold=gold,
                         chunks=tuple(chunks), distractors=distractors)


def load_jsonl(path: str, *, fields: Optional[Mapping[str, str]] = None
               ) -> List[GeneratedTask]:
    """读 JSONL（一行一条）；空行跳过，坏记录直接抛错并指出行号。"""
    tasks: List[GeneratedTask] = []
    seen: set = set()
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetFormatError(f"{path}:{number} 不是合法 JSON：{exc}") from exc
            if not isinstance(record, Mapping):
                raise DatasetFormatError(f"{path}:{number} 不是对象")
            task = from_record(record, fields=fields, source=f"{path}:{number}")
            if task.task_id in seen:
                raise DatasetFormatError(f"{path}:{number} task_id 重复：{task.task_id}")
            seen.add(task.task_id)
            tasks.append(task)
    if not tasks:
        raise DatasetFormatError(f"{path}: 一条记录都没读到")
    return tasks


@dataclass(frozen=True)
class DatasetInfo:
    """加载摘要；报告里要能说清"用的是哪一份、多少条、指纹是什么"。"""

    name: str
    count: int
    fingerprint: str
    source: str = "public"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "count": self.count,
                "fingerprint": self.fingerprint, "source": self.source}


def summarize(tasks: Sequence[GeneratedTask], *, name: str) -> DatasetInfo:
    import hashlib

    blob = json.dumps([task.to_dict() for task in tasks], ensure_ascii=False,
                      sort_keys=True)
    return DatasetInfo(name=name, count=len(tasks),
                       fingerprint=hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16])


def to_records(tasks: Iterable[GeneratedTask]) -> List[Dict[str, Any]]:
    """反向导出（造样例集、或把内部题集喂给外部工具时用）。"""
    return [
        {"id": task.task_id, "question": task.question, "answer": task.gold,
         "contexts": [{"source": chunk.source, "text": chunk.quote}
                      for chunk in task.chunks],
         "distractors": list(task.distractors)}
        for task in tasks
    ]
