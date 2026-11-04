"""可控写工具：一个能在测试里"真的产生副作用"的外部系统替身（设计文档 §11 Phase 3）。

为什么需要它：只读检索工具永远不会产生"部分生效"和"重复副作用"，而这两个正是
`ToolCall` 四档结论要解决的问题。这个替身提供一个**可检查的外部世界**（一个 dict），
并允许按剧本制造几种真实现场：

- `ok`：正常写入；
- `fail`：请求被拒（确定没生效）；
- `apply_then_timeout`：**写进去了，但返回超时**——最典型的 unknown_commit；
- `partial`：写了一半（例如两个字段只写了一个），只能补偿。

配套 `lookup(key)` 让"按幂等键查原结果"这条路能被真实测试；`undo(key)` 是补偿动作。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from atlas.tools.calls import EffectClass, ToolSpec

#: 剧本：决定一次调用在外部世界留下的痕迹。
MODES = ("ok", "fail", "apply_then_timeout", "partial")

WRITE_TOOL_SPEC = ToolSpec(
    name="demo_writer",
    version="1",
    effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
    supports_idempotency=True,
    supports_lookup=True,
    supports_compensation=True,
    timeout_s=5.0,
)


class WriteToolError(RuntimeError):
    """写工具失败：`kind` 与 dispatcher 的分类对齐。"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.message = message


@dataclass
class ControllableWriteTool:
    """内存里的"外部系统"；`world` 可以被测试直接检查。"""

    mode: str = "ok"
    world: Dict[str, Any] = field(default_factory=dict)
    operations: List[str] = field(default_factory=list)
    SPEC = WRITE_TOOL_SPEC

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"未知剧本 {self.mode!r}（可选 {MODES}）")

    # ---------- 对外动作 ----------

    def write(self, key: str, value: Dict[str, Any]) -> Dict[str, Any]:
        """写入；按 `mode` 制造不同的真实后果。"""
        self.operations.append(f"write:{key}:{self.mode}")
        if self.mode == "fail":
            raise WriteToolError("invalid_request", "外部系统拒绝写入")
        if self.mode == "apply_then_timeout":
            # 关键：先真的写进去，再抛超时——这就是"结果未知"的来源
            self.world[key] = dict(value)
            raise WriteToolError("timeout", "写入后响应超时")
        if self.mode == "partial":
            # 只写一半字段：既不是成功也不是没发生
            half = dict(list(value.items())[: max(1, len(value) // 2)])
            self.world[key] = half
            raise WriteToolError("partial", f"只写入了字段 {sorted(half)}")
        self.world[key] = dict(value)
        return {"applied": dict(value)}

    def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        """按幂等键查回结果：对账的第一优先级（§4.8）。"""
        self.operations.append(f"lookup:{key}")
        if key not in self.world:
            return None
        return {"applied": dict(self.world[key])}

    def observe(self, key: str) -> Optional[Dict[str, Any]]:
        """读回目标对象：对账的第二优先级。"""
        self.operations.append(f"observe:{key}")
        return dict(self.world[key]) if key in self.world else None

    def undo(self, key: str) -> Dict[str, Any]:
        """补偿：把 key 对应的副作用撤掉（不做的事就 dot 不到）。"""
        self.operations.append(f"undo:{key}")
        removed = self.world.pop(key, None)
        if removed is None:
            raise WriteToolError("invalid_request", f"{key} 不存在，无法补偿")
        return {"compensated": key, "removed": removed}
