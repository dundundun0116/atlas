"""Atlas adapter（阶段 E）：同一个 ResearchTool 协议的两种实现。

* `LocalResearchTool`：进程内直调，适合开发、单机评测与故障注入；
* `HTTPResearchTool`：走 `POST /research`，适合服务化部署。

两者返回**同一个** `ResearchResult`，所以 Atlas 侧换传输层不需要改业务代码。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..agent.core import Agent
from ..agent.research import research
from ..agent.schemas import ResearchRequest, ResearchResult
from ..api.store import TraceStore


@runtime_checkable
class ResearchTool(Protocol):
    def research(self, request: ResearchRequest) -> ResearchResult: ...
    def get_trace(self, trace_id: str) -> dict[str, Any] | None: ...


class LocalResearchTool:
    """进程内 adapter：与评测/调试共用同一套 Agent 与 graph runner。"""

    def __init__(self, agent: Agent, *, trace_dir: str | None = None,
                 cache: dict[str, ResearchResult] | None = None,
                 use_graph: bool = True) -> None:
        self.agent = agent
        self.store = TraceStore(trace_dir)
        self.cache = cache if cache is not None else {}
        self.use_graph = use_graph

    def research(self, request: ResearchRequest) -> ResearchResult:
        return research(self.agent, request, cache=self.cache,
                        on_trace=self.store.put, use_graph=self.use_graph)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return self.store.get(trace_id)


class HTTPResearchTool:
    """HTTP adapter：只依赖一个 httpx 兼容的 client（测试里可注入 TestClient）。"""

    def __init__(self, base_url: str = "", *, client: Any = None,
                 timeout: float = 30.0) -> None:
        if client is None:
            import httpx
            client = httpx.Client(base_url=base_url, timeout=timeout)
        self.client = client

    def research(self, request: ResearchRequest) -> ResearchResult:
        try:
            resp = self.client.post("/research", json=request.model_dump())
        except Exception:  # noqa: BLE001 - 网络异常也要给结构化失败
            return ResearchResult(failure="timeout")
        if resp.status_code == 422:
            return ResearchResult(failure="invalid_request")
        if resp.status_code >= 500:
            return ResearchResult(failure="upstream_error")
        return ResearchResult.model_validate(resp.json())

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        resp = self.client.get(f"/runs/{trace_id}")
        if resp.status_code != 200:
            return None
        return resp.json().get("trace")

    def health(self) -> dict[str, Any]:
        resp = self.client.get("/health")
        return resp.json() if resp.status_code == 200 else {}


__all__ = ["ResearchTool", "LocalResearchTool", "HTTPResearchTool"]
