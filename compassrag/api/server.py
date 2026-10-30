"""服务入口（阶段 E）：把 `Agent` 包成一个符合 ResearchTool 协议的 HTTP 服务。

用法::

    from compassrag.api.server import create_app
    app = create_app(agent, trace_dir="runs/api_traces")
    # uvicorn compassrag.api.server:create_app --factory
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ..agent.core import Agent
from ..agent.schemas import ResearchResult
from .events import EventPublisher, RunRegistry
from .ingest import IngestStore
from .ingest_routes import build_ingest_router
from .routes import build_router
from .schemas import ErrorBody  # noqa: F401 - 供 OpenAPI 文档引用
from .store import TraceStore


def create_app(agent: Agent, *, trace_dir: str | Path | None = None,
               cache: dict[str, ResearchResult] | None = None,
               ingest_root: str | Path | None = None,
               runtime_index_version: str = "") -> FastAPI:
    store = TraceStore(trace_dir)
    registry, publisher = RunRegistry(), EventPublisher()
    app = FastAPI(title="CompassRAG ResearchTool", version="0.1.0",
                  summary="检索决策 Agent 对外接口：answer + evidence + confidence + stop_reason")
    app.state.agent = agent
    app.state.store = store
    app.state.registry = registry
    app.state.publisher = publisher
    app.include_router(build_router(agent, store=store, cache=cache,
                                    registry=registry, publisher=publisher,
                                    index_version=runtime_index_version))
    if ingest_root is not None:
        app.state.ingest = IngestStore(ingest_root)
        app.include_router(build_ingest_router(app.state.ingest,
                                               runtime_version=runtime_index_version))
    return app


__all__ = ["create_app", "TraceStore"]
