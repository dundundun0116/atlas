"""CompassRAG 适配器：本地函数实现 + HTTP 实现，上层代码不变。

- `LocalResearchTool`：把任意 `fn(request) -> ResearchResult | dict` 包成协议实现
  （本地调用 CompassRAG 的 Python API，或在测试里当替身）；
- `HttpCompassRAGTool`：调 CompassRAG 的服务端接口，负责把 HTTP 状态映射成
  `ResearchToolError` 的五种类型。

两种实现的异常语义一致：调用方只按 `err.kind` 决策，不看底层是进程内还是网络。
"""

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from atlas.tools.protocol import (
    EvidenceItem,
    ResearchRequest,
    ResearchResult,
    ResearchToolError,
)


class LocalResearchTool:
    """本地适配器：注入一个可调用对象即可。"""

    def __init__(self, fn: Callable[[ResearchRequest], Any]):
        self.fn = fn
        self.calls = 0

    def research(self, request: ResearchRequest) -> ResearchResult:
        self.calls += 1
        try:
            payload = self.fn(request)
        except ResearchToolError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ResearchToolError("timeout", str(exc), trace_id=request.trace_id) from exc
        except Exception as exc:  # 本地实现里的一切异常都算上游故障
            raise ResearchToolError(
                "upstream_error", f"{type(exc).__name__}: {exc}", trace_id=request.trace_id
            ) from exc
        if isinstance(payload, ResearchResult):
            return payload
        try:
            return ResearchResult.from_dict(payload)
        except (TypeError, ValueError, KeyError) as exc:
            raise ResearchToolError(
                "invalid_request", f"响应结构不合法：{exc}", trace_id=request.trace_id
            ) from exc


#: 语义别名：本地 CompassRAG 适配器。
CompassRAGLocalTool = LocalResearchTool


class HttpCompassRAGTool:
    """HTTP 适配器：POST {base_url}/research，错误按状态码分类。"""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 30.0,
        opener: Optional[Callable[..., Any]] = None,
        path: str = "/research",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.opener = opener or urllib.request.urlopen
        self.path = path
        self.calls = 0

    def research(self, request: ResearchRequest) -> ResearchResult:
        self.calls += 1
        body = json.dumps(request.to_dict(), ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.base_url}{self.path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": request.trace_id,
                "X-Idempotency-Key": request.idempotency_key,
            },
            method="POST",
        )
        try:
            with self.opener(http_request, timeout=self.timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise ResearchToolError(
                _kind_for_status(exc.code),
                f"HTTP {exc.code}",
                trace_id=request.trace_id,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ResearchToolError("timeout", str(exc), trace_id=request.trace_id) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "upstream_error"
            raise ResearchToolError(kind, str(reason), trace_id=request.trace_id) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchToolError(
                "invalid_request", f"响应不是合法 JSON：{exc}", trace_id=request.trace_id
            ) from exc
        try:
            result = ResearchResult.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise ResearchToolError(
                "invalid_request", f"响应结构不合法：{exc}", trace_id=request.trace_id
            ) from exc
        if not result.trace_id:
            result.trace_id = request.trace_id
        return result


def _kind_for_status(code: int) -> str:
    if code in (400, 422):
        return "invalid_request"
    if code in (408, 504):
        return "timeout"
    if code == 429:
        return "budget_exhausted"
    if code == 404:
        return "no_evidence"
    return "upstream_error"


#: CompassRAG 对外失败词表 → Atlas 五类错误。
_COMPASS_FAILURE_TO_ATLAS = {
    "timeout": "timeout",
    "budget_exhausted": "budget_exhausted",
    "no_evidence": "no_evidence",
    "upstream_error": "upstream_error",
    "invalid_request": "invalid_request",
    # 取消不是 Atlas 的错误分类：按上游中断处理，由任务层决定暂停/重试。
    "cancelled": "upstream_error",
}


class CompassRAGAgentTool:
    """真实进程内桥接：Atlas 直接驱动同仓的 CompassRAG Agent。

    与 `HttpCompassRAGTool` 暴露同一个 `ResearchTool` 协议，但省掉 HTTP 与序列化：

    - 构造时才 import `compassrag`（Atlas 主包不硬依赖检索侧依赖）；
    - 请求/响应在两个 `ResearchRequest` / `ResearchResult` 之间做确定性映射；
    - CompassRAG 的结构化 `failure` 转成 `ResearchToolError` 五类，上层只按 kind 决策；
    - 证据补齐 `retriever` / `rank`，trace_id 原样透传供上层关联。

    幂等与取消语义由 CompassRAG 侧保证（`LocalResearchTool` 的 cache + `cancel_check`）。
    """

    def __init__(self, agent: Any, *, trace_dir: Optional[str] = None,
                 cache: Optional[Dict[str, Any]] = None, use_graph: bool = True):
        try:
            from compassrag.adapters.atlas import LocalResearchTool as _CompassLocal
        except ImportError as exc:  # pragma: no cover - 缺少可选依赖时给出可读错误
            raise ResearchToolError(
                "upstream_error", f"CompassRAG 不可用：{type(exc).__name__}: {exc}"
            ) from exc
        self.agent = agent
        self._inner = _CompassLocal(agent, trace_dir=trace_dir, cache=cache, use_graph=use_graph)
        self.calls = 0

    def research(self, request: ResearchRequest) -> ResearchResult:
        from compassrag.agent.schemas import ResearchRequest as CompassRequest

        self.calls += 1
        try:
            compass_request = CompassRequest(
                session_id=request.session_id,
                subtask_id=request.subtask_id,
                query=request.query,
                budget_tokens=request.budget_tokens,
                max_rounds=request.max_rounds,
                idempotency_key=request.idempotency_key,
            )
            compass_result = self._inner.research(compass_request)
        except ResearchToolError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ResearchToolError("timeout", str(exc), trace_id=request.trace_id) from exc
        except Exception as exc:  # 检索侧内部异常一律按上游故障分类
            raise ResearchToolError(
                "upstream_error", f"{type(exc).__name__}: {exc}", trace_id=request.trace_id
            ) from exc

        trace_id = compass_result.trace_id or request.trace_id
        if compass_result.failure:
            kind = _COMPASS_FAILURE_TO_ATLAS.get(compass_result.failure, "upstream_error")
            raise ResearchToolError(
                kind, f"CompassRAG {compass_result.failure}", trace_id=trace_id
            )
        return ResearchResult(
            answer=compass_result.answer,
            evidence=[
                EvidenceItem(
                    source=item.source or item.chunk_id or "unknown",
                    chunk_id=item.chunk_id,
                    quote=item.quote,
                    score=float(item.score or 0.0),
                    retriever=item.retriever,
                    rank=int(item.rank or 0),
                )
                for item in compass_result.evidence
            ],
            confidence=float(compass_result.confidence or 0.0),
            retrieval_rounds=int(compass_result.retrieval_rounds or 0),
            stop_reason=compass_result.stop_reason,
            trace_id=trace_id,
        )

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """回读 CompassRAG 的单次研究轨迹（不重新调用模型）。"""
        return self._inner.get_trace(trace_id)


def describe_failure(exc: ResearchToolError) -> Dict[str, Any]:
    """把工具失败压成可入 trace / 事件的字典。"""
    return {"kind": exc.kind, "message": exc.message, "trace_id": exc.trace_id}
