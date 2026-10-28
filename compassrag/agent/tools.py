"""统一检索工具协议（M1）。

现状：四个检索器（`retrieval/bm25.py`、`retrieval/dense.py`、`index/kg.py`、
`index/wiki.py`）签名已经一致（`search(query, k) -> [(chunk_id, score)]`），但

- 输出丢掉了 `retriever` 与 `rank`；
- 三路 score 语义完全不同（BM25 原始分 / 余弦相似度 / PPR 分数），只有走 RRF 才没暴露；
- 抓近重合并时 `agent.py` 要 `getattr(tools["dense"], "vector", None)` 反查内部方法；
- 任何异常都直接抛穿 `Agent.answer()`，一题出错整轮评测中断。

本模块把工具适配成同一个 `ToolSpec.run(SearchRequest) -> ToolCall`：

* 输入校验（空 query / k 越界）由 `SearchRequest` 把关；
* **异常不外泄**：失败落成 `outcome`（`error` / `timeout` / `invalid_params` /
  `index_missing`），由控制流决定降级还是继续用别的工具；
* 记录 `retriever` 与 `rank`，`score` 原样保留。

超时默认关闭（进程内 numpy/bm25s 检索没有真实可中断点）。开启后通过线程池
`future.result(timeout=...)` 强制返回，慢工具不会拖死整题。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Iterable, Iterator

from .schemas import SearchRequest, ToolCall, ToolHit

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="compassrag-tool")


class ToolError(RuntimeError):
    """工具层错误的基类：控制流据此分类，而不是靠猜异常类型。"""

    outcome = "error"


class ToolInvalidParams(ToolError):
    outcome = "invalid_params"


class ToolTimeout(ToolError):
    outcome = "timeout"


class ToolIndexMissing(ToolError):
    outcome = "index_missing"


def _normalize_hits(raw: Any, retriever: str) -> list[ToolHit]:
    """把 `[(cid, score)]` / `[{"chunk_id":…, "score":…}]` / `[ToolHit]` 统一成 ToolHit 列表。"""
    if raw is None:
        return []
    hits: list[ToolHit] = []
    for i, item in enumerate(raw):
        if isinstance(item, ToolHit):
            hits.append(item.model_copy(update={"retriever": item.retriever or retriever,
                                                "rank": item.rank or i + 1}))
        elif isinstance(item, dict):
            cid = item.get("chunk_id") or item.get("id")
            if not cid:
                raise ToolInvalidParams(f"工具 {retriever} 返回的 dict 缺少 chunk_id: {item!r}")
            hits.append(ToolHit(chunk_id=str(cid), score=float(item.get("score", 0.0)),
                                retriever=retriever, rank=int(item.get("rank", i + 1))))
        else:
            try:
                cid, score = item[0], item[1]
            except Exception as exc:  # noqa: BLE001 - 形状非法要变成工具错误而不是崩溃
                raise ToolInvalidParams(
                    f"工具 {retriever} 返回项不是 (chunk_id, score): {item!r}") from exc
            hits.append(ToolHit(chunk_id=str(cid), score=float(score),
                                retriever=retriever, rank=i + 1))
    return hits


class ToolSpec:
    """把一个检索器对象适配成统一协议。

    只要求底层对象有 `search(query, k=...)`；有 `vector(cid)` 时一并代理（供近重合并）。
    """

    def __init__(self, name: str, tool: Any, *, timeout_s: float = 0.0,
                 missing_hint: str = "") -> None:
        if not hasattr(tool, "search"):
            raise ToolInvalidParams(f"工具 {name} 缺少 search(query, k) 方法")
        self.name = name
        self.tool = tool
        self.timeout_s = float(timeout_s or 0.0)
        self.missing_hint = missing_hint

    # -- 协议 -------------------------------------------------------------- #

    def run(self, request: SearchRequest) -> ToolCall:
        call = ToolCall(tool_name=self.name, request=request)
        t0 = time.perf_counter()
        try:
            raw, outcome, error = self._invoke(request)
        except FutureTimeout as exc:
            outcome, raw, error = "timeout", None, str(exc)
        except ToolError as exc:
            outcome, raw, error = exc.outcome, None, str(exc)
        except FileNotFoundError as exc:      # 索引/图未构建
            outcome, raw, error = "index_missing", None, str(exc)
        except (TypeError, ValueError) as exc:  # 参数或返回形状不合法
            outcome, raw, error = "invalid_params", None, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - 工具错误绝不能把整题拖死
            outcome, raw, error = "error", None, f"{type(exc).__name__}: {exc}"
        call.latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        if raw is not None:
            try:
                call.hits = _normalize_hits(raw, self.name)
                outcome = "ok" if call.hits else "empty"
            except ToolError as exc:
                outcome, error = exc.outcome, str(exc)
        if outcome == "empty" and not call.hits:
            error = error or "no hits"
            if self.missing_hint:
                error += f"（{self.missing_hint}）"
        call.outcome = outcome  # type: ignore[assignment]
        call.error = error
        return call

    def _invoke(self, request: SearchRequest) -> tuple[Any, str, str]:
        """返回 (原始结果, 占位 outcome, 占位 error)，异常交给 run 统一分类。"""
        if self.timeout_s <= 0:
            return self.tool.search(request.query, k=request.top_k), "ok", ""
        future = _EXECUTOR.submit(self.tool.search, request.query, request.top_k)
        try:
            return future.result(timeout=self.timeout_s), "ok", ""
        except FutureTimeout:
            future.cancel()
            raise ToolTimeout(f"{self.name} 超时 {self.timeout_s}s") from None

    # -- 兼容旧路径 --------------------------------------------------------- #

    def vector(self, chunk_id: str) -> Any:
        """近重合并需要向量时用；没有就返回 None（不再反查工具内部结构）。"""
        getter = getattr(self.tool, "vector", None)
        return getter(chunk_id) if callable(getter) else None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ToolSpec({self.name}, timeout_s={self.timeout_s})"


class ToolRegistry:
    """按插入顺序保存工具；顺序影响 RRF 的平局次序，必须稳定。"""

    def __init__(self, tools: dict[str, Any] | Iterable[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        if isinstance(tools, dict):
            for name, tool in tools.items():
                self.register(name, tool)
        elif tools is not None:
            for spec in tools:
                self.register(spec.name, spec)

    def register(self, name: str, tool: Any, *, timeout_s: float | None = None) -> ToolSpec:
        spec = tool if isinstance(tool, ToolSpec) else ToolSpec(
            name, tool, timeout_s=self.timeout_s if timeout_s is None else timeout_s)
        self._specs[name] = spec
        return spec

    @property
    def timeout_s(self) -> float:
        return float(getattr(self, "_timeout_s", 0.0))

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        self._timeout_s = float(value or 0.0)
        for spec in self._specs.values():
            spec.timeout_s = float(value or 0.0)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: str) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return list(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def run_all(self, request: SearchRequest, *,
                names: list[str] | None = None) -> list[ToolCall]:
        """一条查询打所有工具（或指定子集）；**任何一个失败都不影响其他工具**。

        子集用于"切换检索视图"式修复：只走实体图或只走向量，而不是把三路全打一遍。
        """
        specs = ([self._specs[n] for n in names if n in self._specs]
                 if names else list(self._specs.values()))
        return [spec.run(request) for spec in specs]


def build_registry(tools: dict[str, Any], *, timeout_s: float = 0.0) -> ToolRegistry:
    """从旧的 `{name: 检索器对象}` 映射构造注册表（兼容既有调用点）。"""
    reg = ToolRegistry()
    reg._timeout_s = float(timeout_s or 0.0)
    for name, tool in tools.items():
        reg.register(name, tool, timeout_s=timeout_s)
    return reg


__all__ = [
    "ToolError", "ToolInvalidParams", "ToolIndexMissing", "ToolTimeout",
    "ToolSpec", "ToolRegistry", "build_registry", "_normalize_hits",
]
