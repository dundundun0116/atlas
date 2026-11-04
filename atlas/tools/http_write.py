"""真实写工具适配器：把**外部 HTTP 服务**接到 ToolCall 协议上（设计文档 §6 / §8 / §11）。

`ControllableWriteTool` 证明的是协议本身；这个适配器证明协议**接上真实网络**之后还成立。
真实网络不会把结果分成"成功 / 失败"两档——超时、连接被重置、5xx、服务端幂等重放，
每一种都落在四档结论的不同格子里，而**分错格的代价是重复副作用**。所以这里只做三件事：

1. **能力是声明出来的，不是猜出来的。** `ServiceProfile` 写明远端到底支不支持
   `Idempotency-Key`、有没有"按幂等键查原结果"的端点、能不能撤销。工具只声明它真有的能力；
   没有的能力，对账就只能得出 `unknowable`，交给人——这不是保守，是诚实的上限。
2. **判类只看"请求有没有可能被受理"。** 2xx = 已生效；4xx（除 408/425）= 确定没生效；
   5xx / 超时 / 连接中断 = **不知道**，一律 `unknown_commit`，绝不当作失败重发。
3. **幂等键走请求头。** 服务端认这个头，同一个键重发才是安全的；不认，重发就是新的一笔。

两个现成的 profile 覆盖了现实里的两代服务：

    IDEMPOTENT_PROFILE  支持 Idempotency-Key + 按键查结果 + 可撤销   → 能自动对账、能自动补偿
    LEGACY_PROFILE      什么都不支持，只能读回对象                   → 未知只能升级给人 + 派发前要审批

只用标准库（`urllib`），不引入新依赖。
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from atlas.tools.calls import EffectClass, ToolSpec

#: 这些状态码说明服务端**明确拒绝受理**：请求没进业务逻辑，动作没发生。
_REJECTED_STATUSES = frozenset({
    400, 401, 403, 404, 405, 406, 410, 411, 412, 413, 414, 415, 422, 425, 429,
})

#: 这些状态码说明"请求可能已经被处理，只是结果没回来"——不能当失败。
_UNKNOWN_STATUSES = frozenset({408, 500, 501, 502, 503, 504, 507, 508, 599})


class HttpWriteError(RuntimeError):
    """写工具失败；`kind` 与 `ToolDispatcher` 的分类表对齐。"""

    def __init__(self, kind: str, message: str = "", *,
                 status: Optional[int] = None, body: Any = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message
        self.status = status
        self.body = body


@dataclass(frozen=True)
class ServiceProfile:
    """远端服务的能力声明。

    这里的每个字段都要能对应到一条**真实存在**的远端能力，不许"应该支持吧"：
    声明了却做不到，工具就会在错的时候给出 `applied`，那比不知道更糟。
    """

    name: str = "generic"
    #: 幂等键请求头；`None` = 服务端不认，重发不安全。
    idempotency_header: Optional[str] = "Idempotency-Key"
    #: 按幂等键查原结果的端点（`{key}` 会被 URL 转义后填入）；`None` = 不支持。
    lookup_template: Optional[str] = "/idempotency/{key}"
    #: 读回目标对象；`None` = 连读回都做不到（那对账就只能靠人）。
    observe_template: Optional[str] = "/objects/{target}"
    #: 撤销目标对象；`None` = 不可补偿。
    undo_template: Optional[str] = "/objects/{target}"
    #: 服务端用 409 表示"这个幂等键之前成功过"（Stripe 系语义）。
    #: 默认不这么认：409 也可能只是"对象已存在，跟这次请求无关"——
    #: 认错了会把别人的写入记成自己的，所以默认按"不知道"处理。
    treat_409_as_applied: bool = False
    #: 单次请求超时（秒）。超过就是"不知道"，不是"失败"。
    timeout_s: float = 5.0

    @property
    def supports_idempotency(self) -> bool:
        return bool(self.idempotency_header)

    @property
    def supports_lookup(self) -> bool:
        return bool(self.lookup_template)

    @property
    def supports_compensation(self) -> bool:
        return bool(self.undo_template)

    def tool_spec(self, name: str = "http_writer", version: str = "1") -> ToolSpec:
        """能力 → ToolSpec。缺省一律最保守（副作用型、不支持、不可补偿）。"""
        return ToolSpec(
            name=name,
            version=version,
            effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
            supports_idempotency=self.supports_idempotency,
            supports_lookup=self.supports_lookup,
            supports_compensation=self.supports_compensation,
            timeout_s=self.timeout_s,
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "idempotency_header": self.idempotency_header,
            "lookup": bool(self.lookup_template),
            "observe": bool(self.observe_template),
            "undo": bool(self.undo_template),
        }


#: 新一代服务：可去重、可查询、可撤销——未知可以自动收敛。
IDEMPOTENT_PROFILE = ServiceProfile(name="idempotent_service")

#: 老一代服务：不认幂等键、没有查询端点、不能撤销。
#: 结果未知时唯一的出路是人；因此**派发之前**就必须过人工闸门。
LEGACY_PROFILE = ServiceProfile(
    name="legacy_service",
    idempotency_header=None,
    lookup_template=None,
    observe_template="/objects/{target}",
    undo_template=None,
)


def classify_status(status: int, profile: ServiceProfile) -> Optional[str]:
    """HTTP 状态码 → 错误 kind；`None` 表示这次请求算成功。

    分类只回答一个问题：**请求有没有可能已经被受理？**
    答"可能"就绝不给 `invalid_request`——那会让上层放心重试。
    """
    if 200 <= status < 300:
        return None
    if status == 409 and profile.treat_409_as_applied:
        return None
    if status == 408:
        # 代理层说的话：上游到底做没做没人知道——按超时处理，不是"没生效"
        return "timeout"
    if status in _UNKNOWN_STATUSES or status >= 500:
        return "upstream_error"
    if status in _REJECTED_STATUSES:
        return "invalid_request"
    # 没见过的状态码：宁可按"不知道"处理
    return "unknown_commit"


@dataclass
class HttpWriteTool:
    """把远端 HTTP 服务当成一个可对账的写工具。

    `key` 是**幂等键**（由 `atlas.tools.calls.idempotency_key` 生成），不是业务主键；
    `target` 才是业务主键。两个都要，因为"同一个逻辑操作只做一次"靠 key，
    "读回哪个对象"靠 target。
    """

    base_url: str = "http://127.0.0.1:0"
    profile: ServiceProfile = field(default_factory=lambda: IDEMPOTENT_PROFILE)
    #: 测试注入点；默认走 `urllib.request.urlopen`。
    opener: Optional[Callable[..., Any]] = None
    #: 记录真正发出的请求，便于断言"没有重发"。
    requests: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("base_url 不能为空")

    #: 告诉调度层"我要幂等键"——真实网络工具必须能把它放进请求头。
    wants_idempotency_key: bool = True

    # ---------- 能力 ----------

    @property
    def SPEC(self) -> ToolSpec:  # noqa: N802 - 与 ControllableWriteTool 的注入约定一致
        return self.profile.tool_spec(name=f"http_writer[{self.profile.name}]")

    # ---------- 对外动作 ----------

    def write(self, target: str, value: Dict[str, Any], *,
              idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """写入一个对象。成功返回 `{"applied": {...}}`，否则抛 `HttpWriteError`。

        `target` 是**业务主键**（写哪个对象），`idempotency_key` 是**去重键**
        （同一逻辑操作只做一次）；两者不是一回事，混用会让重放对不上。
        """
        target = str(target or "").strip()
        if not target:
            raise HttpWriteError("invalid_request", "target 不能为空")
        path = f"/objects/{_quote(target)}"
        headers = {"Content-Type": "application/json"}
        if self.profile.supports_idempotency and idempotency_key:
            headers[str(self.profile.idempotency_header)] = str(idempotency_key)

        status, payload = self._request("PUT", path, body=dict(value or {}),
                                        headers=headers)
        if payload.get("partial"):
            raise HttpWriteError("partial", "服务端只写入了一部分字段",
                                 status=status, body=payload)
        kind = classify_status(status, self.profile)
        if kind is not None:
            raise HttpWriteError(kind, _message(payload, status),
                                 status=status, body=payload)
        applied = payload.get("applied")
        return {"applied": dict(applied) if isinstance(applied, dict) else dict(payload)}

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        """按幂等键查原结果——对账的第一优先级。

        服务端不支持时抛 `unsupported`：**不要让调用方以为查过了**。
        """
        if not self.profile.supports_lookup:
            raise HttpWriteError("unsupported",
                                 f"{self.profile.name} 不提供按幂等键查询的端点")
        status, payload = self._request(
            "GET", self._render(self.profile.lookup_template, key=key))
        if status == 404:
            return None
        kind = classify_status(status, self.profile)
        if kind is not None:
            raise HttpWriteError(kind, _message(payload, status),
                                 status=status, body=payload)
        applied = payload.get("applied")
        return {"applied": dict(applied) if isinstance(applied, dict) else dict(payload)}

    def observe(self, target: str) -> Optional[Dict[str, Any]]:
        """读回目标对象——对账的第二优先级，也是 `confirm` 步的来源。"""
        if not self.profile.observe_template:
            raise HttpWriteError("unsupported",
                                 f"{self.profile.name} 不支持读回对象")
        status, payload = self._request(
            "GET", self._render(self.profile.observe_template, target=target))
        if status == 404:
            return None
        kind = classify_status(status, self.profile)
        if kind is not None:
            raise HttpWriteError(kind, _message(payload, status),
                                 status=status, body=payload)
        value = payload.get("value")
        return dict(value) if isinstance(value, dict) else dict(payload)

    def undo(self, target: str) -> Dict[str, Any]:
        """补偿：撤掉这个对象的副作用。"""
        if not self.profile.supports_compensation:
            raise HttpWriteError("unsupported",
                                 f"{self.profile.name} 不支持撤销，无法自动补偿")
        status, payload = self._request(
            "DELETE", self._render(self.profile.undo_template, target=target))
        if status == 404:
            raise HttpWriteError("invalid_request", f"{target} 不存在，无法补偿",
                                 status=status, body=payload)
        kind = classify_status(status, self.profile)
        if kind is not None:
            raise HttpWriteError(kind, _message(payload, status),
                                 status=status, body=payload)
        return {"compensated": target, "removed": payload.get("removed")}

    # ---------- 传输 ----------

    def _render(self, template: Optional[str], **values: str) -> str:
        assert template is not None  # 调用方已按能力判过
        return template.format(**{k: _quote(str(v)) for k, v in values.items()})

    def _request(self, method: str, path: str, *,
                 body: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None):
        """发一次请求并返回 `(status, parsed_payload)`；网络层异常一律转成 unknown。

        关键约定：**这里不抛"失败"给上层**——只有拿到响应才谈得上分类；
        拿不到响应的，全部是 `timeout` / `unknown_commit`，因为请求已经出去了。
        """
        url = f"{self.base_url}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=dict(headers or {}))
        self.requests.append({"method": method, "url": url,
                              "headers": dict(headers or {})})
        open_fn = self.opener or urllib.request.urlopen
        try:
            with open_fn(request, timeout=self.profile.timeout_s) as response:
                return int(getattr(response, "status", response.getcode())), _decode(response)
        except urllib.error.HTTPError as exc:  # 有响应，只是状态码不是 2xx
            return int(exc.code), _decode(exc)
        except (socket.timeout, TimeoutError) as exc:
            raise HttpWriteError("timeout", f"请求超时：{exc}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise HttpWriteError("timeout", f"请求超时：{reason}") from exc
            raise HttpWriteError("unknown_commit",
                                 f"连接中断，无法确认远端是否已执行：{reason}") from exc
        except OSError as exc:
            raise HttpWriteError("unknown_commit",
                                 f"网络错误，无法确认远端是否已执行：{exc}") from exc


def _quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        for field_name in ("error", "message", "detail"):
            if payload.get(field_name):
                return str(payload[field_name])
    return f"HTTP {status}"


def _decode(response) -> Dict[str, Any]:
    """把响应体解成对象；不是 JSON 就原样塞进 `{"raw": ...}`。"""
    try:
        raw = response.read()
    except Exception:  # noqa: BLE001 - 读响应体失败不该掩盖状态码
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw.decode("utf-8", "replace")}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
