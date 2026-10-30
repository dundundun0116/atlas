"""ResearchTool 服务化（阶段 E）：HTTP 接口 + 本地 adapter 共用同一份契约。"""

from .ingest import IngestStore
from .ingest_routes import build_ingest_router
from .routes import build_router
from .server import create_app
from .store import TraceStore

__all__ = ["build_router", "create_app", "TraceStore", "IngestStore", "build_ingest_router"]
