"""Task 4 的 HTTP 层：上传文档、构建索引版本、查询状态。

索引版本对运行是**只读快照**：`runtime_version` 是服务启动时装载的那一版；重建只产生新版本，
不会偷偷替换正在跑的实例，`/index/status` 会把"最新构建"和"运行时在用"并排给出。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .ingest import DocumentIn, DocumentMeta, IndexVersion, IngestStore


class BuildRequest(BaseModel):
    dense: bool = False
    entity_graph: bool = False


class IndexStatus(BaseModel):
    runtime_version: str = ""
    built_version: str = ""
    mismatch: bool = False
    n_documents: int = 0
    n_chunks: int = 0
    n_versions: int = 0
    last_error: str = ""


class DocumentUploadResult(BaseModel):
    meta: DocumentMeta
    duplicate: bool = False


def build_ingest_router(store: IngestStore, *, runtime_version: str = "") -> APIRouter:
    router = APIRouter()

    @router.post("/documents", response_model=DocumentUploadResult, status_code=201)
    def upload(payload: DocumentIn) -> DocumentUploadResult:
        try:
            meta, duplicate = store.put_document(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail={"error": "invalid_request", "detail": str(exc)})
        return DocumentUploadResult(meta=meta, duplicate=duplicate)

    @router.get("/documents", response_model=list[DocumentMeta])
    def list_documents() -> list[DocumentMeta]:
        return store.documents()

    @router.post("/index/build", response_model=IndexVersion)
    def build(req: BuildRequest | None = None) -> IndexVersion:
        req = req or BuildRequest()
        version = store.build(dense=req.dense, entity_graph=req.entity_graph)
        if version.error == "no_documents":
            raise HTTPException(status_code=409,
                                detail={"error": "no_documents",
                                        "detail": "先上传文档再构建索引"})
        return version

    @router.get("/index/versions", response_model=list[IndexVersion])
    def versions() -> list[IndexVersion]:
        return store.versions()

    @router.get("/index/status", response_model=IndexStatus)
    def status() -> IndexStatus:
        latest = store.latest_version()
        docs = store.documents()
        return IndexStatus(
            runtime_version=runtime_version,
            built_version=latest.version if latest else "",
            mismatch=bool(latest and runtime_version and latest.version != runtime_version),
            n_documents=len(docs),
            n_chunks=latest.n_chunks if latest else 0,
            n_versions=len(store.versions()),
            last_error=latest.error if latest else "",
        )

    return router


__all__ = ["build_ingest_router", "BuildRequest", "IndexStatus", "DocumentUploadResult"]
