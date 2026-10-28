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


def describe_failure(exc: ResearchToolError) -> Dict[str, Any]:
    """把工具失败压成可入 trace / 事件的字典。"""
    return {"kind": exc.kind, "message": exc.message, "trace_id": exc.trace_id}
