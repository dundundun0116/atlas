"""语料上传与索引版本（Task 4）。

四件事，刻意保持可观察、可回滚：

* **文档**：`name + content + format` 落盘，`fingerprint = sha1(name\\0content)`；重复上传不报错，
  直接返回既有文档并标记 `duplicate`。
* **构建**：每次生成一个新版本 `v{YYYYmmddHHMMSS}`，产物是 `chunks.jsonl` + BM25 索引；
  稠密/实体图阶段先标 `skipped`（需要本地模型/付费抽取），接口与状态位已经留好。
* **状态**：`/index/status` 同时给 `built_version`（最新构建）与 `runtime_version`（服务启动时装的
  那一版）——两者不一致就说明"你看到的数字和跑出来的不是同一版索引"。
* **固定版本**：运行在提交时把 `runtime_version` 写进 RunRecord，索引重建不影响正在跑的任务。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, field_validator, model_validator

from ..corpus.chunking import chunk_id, chunk_text
from ..retrieval.bm25 import BM25Index


class DocumentIn(BaseModel):
    name: str
    content: str
    format: str = "txt"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name 不能为空")
        return v

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        v = (v or "txt").strip().lower()
        if v not in ("txt", "md", "json"):
            raise ValueError("format 只支持 txt / md / json")
        return v

    @field_validator("content")
    @classmethod
    def _content(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("content 不能为空")
        return v

    @model_validator(mode="after")
    def _json_parsable(self):
        if self.format == "json":
            try:
                payload = json.loads(self.content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"json 解析失败：{exc}") from exc
            items = payload if isinstance(payload, list) else [payload]
            if not any(isinstance(i, dict) and "text" in i for i in items):
                raise ValueError("json 需要 {title, text} 或它们的数组")
        return self


class DocumentMeta(BaseModel):
    doc_id: str
    name: str
    format: str
    fingerprint: str
    size: int
    created_at: float


class BuildStage(BaseModel):
    name: str
    status: str = "pending"      # pending | running | ok | skipped | failed
    detail: str = ""
    elapsed_ms: float = 0.0


class IndexVersion(BaseModel):
    version: str
    created_at: float
    n_documents: int = 0
    n_chunks: int = 0
    stages: list[BuildStage] = Field(default_factory=list)
    error: str = ""


def _parse_document(meta: DocumentMeta, content: str) -> list[dict]:
    """按格式拆成 {title, text}；json 支持单对象或数组。"""
    if meta.format == "json":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"json 解析失败：{exc}") from exc
        items = payload if isinstance(payload, list) else [payload]
        docs: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or "text" not in item:
                raise ValueError("json 需要 {title, text} 或它们的数组")
            docs.append({"title": str(item.get("title") or meta.name),
                         "text": str(item["text"])})
        return docs
    if meta.format == "md":
        # 用第一个标题当 title，其余作为正文；没有标题就用文件名
        lines = content.splitlines()
        title = meta.name
        for i, line in enumerate(lines):
            if line.startswith("#"):
                title = line.lstrip("#").strip() or meta.name
                lines = lines[:i] + lines[i + 1:]
                break
        return [{"title": title, "text": "\n".join(lines)}]
    return [{"title": meta.name, "text": content}]


class IngestStore:
    """文档与索引版本的文件层；不持有 Agent，也不做网络调用。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.docs_dir = self.root / "documents"
        self.versions_dir = self.root / "versions"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    # -- 文档 ------------------------------------------------------------ #

    def _doc_path(self, doc_id: str) -> Path:
        return self.docs_dir / f"{doc_id}.json"

    def put_document(self, payload: DocumentIn) -> tuple[DocumentMeta, bool]:
        fingerprint = hashlib.sha1(
            f"{payload.name}\0{payload.content}".encode("utf-8")).hexdigest()
        doc_id = fingerprint[:12]
        path = self._doc_path(doc_id)
        if path.exists():
            return DocumentMeta(**json.loads(path.read_text(encoding="utf-8"))["meta"]), True
        meta = DocumentMeta(doc_id=doc_id, name=payload.name, format=payload.format,
                            fingerprint=fingerprint, size=len(payload.content),
                            created_at=time.time())
        path.write_text(json.dumps({"meta": meta.model_dump(), "content": payload.content},
                                   ensure_ascii=False), encoding="utf-8")
        return meta, False

    def documents(self) -> list[DocumentMeta]:
        out = []
        for path in sorted(self.docs_dir.glob("*.json")):
            try:
                out.append(DocumentMeta(**json.loads(path.read_text(encoding="utf-8"))["meta"]))
            except Exception:  # noqa: BLE001 - 坏文件跳过而不是整个列表挂掉
                continue
        return out

    def document_content(self, doc_id: str) -> str | None:
        path = self._doc_path(doc_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["content"]

    # -- 版本 ------------------------------------------------------------ #

    def versions(self) -> list[IndexVersion]:
        out = []
        for path in sorted(self.versions_dir.glob("*/version.json")):
            try:
                out.append(IndexVersion(**json.loads(path.read_text(encoding="utf-8"))))
            except Exception:  # noqa: BLE001
                continue
        return out

    def latest_version(self) -> IndexVersion | None:
        versions = self.versions()
        return versions[-1] if versions else None

    def build(self, *, version: str | None = None,
              dense: bool = False, entity_graph: bool = False) -> IndexVersion:
        """构建一个新版本。阶段状态如实记录，跳过也写原因。"""
        doc_metas = self.documents()
        base = version or time.strftime("v%Y%m%d%H%M%S")
        version, n = base, 1
        while (self.versions_dir / version).exists():   # 同一秒内连续构建要能区分
            n += 1
            version = f"{base}-{n}"
        out_dir = self.versions_dir / version
        out_dir.mkdir(parents=True, exist_ok=True)
        stages = [BuildStage(name="chunk"), BuildStage(name="bm25"),
                  BuildStage(name="dense"), BuildStage(name="entity_graph")]
        result = IndexVersion(version=version, created_at=time.time(),
                              n_documents=len(doc_metas), stages=stages)

        if not doc_metas:
            stages[0].status = "failed"
            stages[0].detail = "没有文档可构建"
            result.error = "no_documents"
            self._save(out_dir, result)
            return result

        chunks: list[dict] = []
        t0 = time.perf_counter()
        stages[0].status = "running"
        try:
            for meta in doc_metas:
                content = self.document_content(meta.doc_id) or ""
                for piece in _parse_document(meta, content):
                    for sub in chunk_text(piece["text"]):
                        chunks.append({"chunk_id": chunk_id(piece["title"], sub),
                                       "title": piece["title"], "text": sub,
                                       "doc_id": meta.doc_id})
            stages[0].status = "ok"
            stages[0].detail = f"{len(doc_metas)} 文档 → {len(chunks)} 块"
        except Exception as exc:  # noqa: BLE001
            stages[0].status = "failed"
            stages[0].detail = f"{type(exc).__name__}: {exc}"
            result.error = "chunk_failed"
        stages[0].elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        if chunks:
            (out_dir / "chunks.jsonl").write_text(
                "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n",
                encoding="utf-8")
            t0 = time.perf_counter()
            stages[1].status = "running"
            try:
                BM25Index.build(chunks).save(out_dir / "bm25")
                stages[1].status = "ok"
                stages[1].detail = f"{len(chunks)} 块"
            except Exception as exc:  # noqa: BLE001
                stages[1].status = "failed"
                stages[1].detail = f"{type(exc).__name__}: {exc}"
                result.error = result.error or "bm25_failed"
            stages[1].elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        # The ingest path currently materializes BM25 only.  Do not report
        # optional stages as successful merely because the caller requested them.
        stages[2].status = "skipped"
        stages[2].detail = "未实现：上传文档索引暂不构建向量"
        stages[3].status = "skipped"
        stages[3].detail = "未实现：上传文档索引暂不构建实体图"

        result.n_chunks = len(chunks)
        if not result.error and not any(s.status == "ok" for s in stages[1:2]):
            result.error = "index_failed"
        self._save(out_dir, result)
        return result

    @staticmethod
    def _save(out_dir: Path, result: IndexVersion) -> None:
        (out_dir / "version.json").write_text(result.model_dump_json(indent=2),
                                              encoding="utf-8")

    # -- 检索辅助 -------------------------------------------------------- #

    def load_tools(self, version: str) -> tuple[dict, dict]:
        """把某个版本装成 `{name: 检索器}` + `chunk_store`。

        只装 BM25：上传的文档没有向量与实体图（构建阶段标 skipped），所以这个版本
        只支持词法检索——这一点必须写进状态，不能让调用方以为三路都在。
        """
        version_dir = self.versions_dir / version
        if not (version_dir / "bm25").exists():
            raise ValueError(f"版本 {version} 没有 BM25 索引，无法装载")
        chunks = self.load_chunks(version)
        store = {c["chunk_id"]: c for c in chunks}
        tools = {"bm25": BM25Index.load(version_dir / "bm25")}
        return tools, store

    def load_chunks(self, version: str) -> list[dict]:
        path = self.versions_dir / version / "chunks.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


__all__ = ["DocumentIn", "DocumentMeta", "BuildStage", "IndexVersion", "IngestStore"]
