"""ContextPackage：一次模型调用**实际看到**的上下文，可复盘、可下钻。

设计文档 §4.12 要求每次模型调用都保存「包含了什么 / 排除了什么 / 用了多少 token」。
这里把四区组装器（`atlas.context.assembler`）的输出包成一份结构化记录：

- `digest`：上下文的 sha256 指纹，trace 与 checkpoint 用它对齐「这一次调用的输入」；
- `sections`：每个区的 token 数（预算怎么花的）；
- `included_refs`：正文里真正出现的节点 id（模型能引用到的状态）；
- `excluded_refs`：被 provenance 挡掉的节点与原因（模型看不到的状态，必须留痕）；
- `degraded` / `warnings`：组装不变量被破坏时的降级事实。

**只存指纹与引用，不存正文**：正文由「同一图快照 + 同一预算」可重放，存全文会把
状态库变成日志库（设计文档 §13 的取舍）。
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from atlas.context.assembler import AuditError, ContextAssembler, PlanZoneOverflow

#: 单次上下文预算的默认值（token）。可由调用方/部署覆盖。
DEFAULT_BUDGET_TOKENS = 4000

#: API 编排路径只渲染计划区 + 提示区：这条路径没有"结转摘要 / 原始记录缓冲"。
DEFAULT_ZONES: Tuple[str, ...] = ("hints",)

_TOKENIZER_NAME = "tiktoken:cl100k_base"
_FALLBACK_TOKENIZER_NAME = "heuristic:chars/4"
_ID_PATTERN = re.compile(r"\[([A-Za-z]{1,3}\d+)\]")

_counter_cache: Dict[str, Callable[[str], int]] = {}


def default_token_counter() -> Tuple[Callable[[str], int], str]:
    """返回 (计数器, 名称)。tiktoken 不可用时退化为字符数启发式，**并如实标注**。"""
    cached = _counter_cache.get(_TOKENIZER_NAME)
    if cached is not None:
        return cached, _TOKENIZER_NAME
    try:  # pragma: no cover - 依赖本机是否预置了 BPE 文件
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")

        def count(text: str) -> int:
            return len(encoding.encode(text or ""))

        _counter_cache[_TOKENIZER_NAME] = count
        return count, _TOKENIZER_NAME
    except Exception:
        def heuristic(text: str) -> int:
            text = text or ""
            return (len(text) + 3) // 4 if text else 0

        return heuristic, _FALLBACK_TOKENIZER_NAME


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ContextPackage:
    """一次组装的完整记录；`text` 是喂给模型的部分，其余是给审计用的。"""

    role: str
    session_id: str = ""
    context_id: str = ""
    state_version: int = 0
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    actual_tokens: int = 0
    tokenizer: str = ""
    digest: str = ""
    text: str = ""
    sections: Dict[str, int] = field(default_factory=dict)
    included_refs: List[str] = field(default_factory=list)
    excluded_refs: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    degraded: bool = False
    created_at: str = ""

    def to_dict(self, include_text: bool = False) -> Dict[str, Any]:
        payload = {
            "context_id": self.context_id,
            "session_id": self.session_id,
            "role": self.role,
            "state_version": self.state_version,
            "budget_tokens": self.budget_tokens,
            "actual_tokens": self.actual_tokens,
            "tokenizer": self.tokenizer,
            "digest": self.digest,
            "sections": dict(self.sections),
            "included_refs": list(self.included_refs),
            "excluded_refs": list(self.excluded_refs),
            "warnings": list(self.warnings),
            "degraded": self.degraded,
            "created_at": self.created_at,
        }
        if include_text:
            payload["text"] = self.text
        return payload


def digest_text(text: str) -> str:
    """上下文指纹：sha256 前 32 位，足够对齐又不占空间。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def _conflict_warnings(provenance_report: Optional[Dict[str, Any]]) -> List[str]:
    if not provenance_report:
        return []
    warnings: List[str] = []
    for conflict in provenance_report.get("conflicts") or []:
        ids = "、".join(str(i) for i in conflict.get("node_ids") or [])
        warnings.append(f"约束冲突（key={conflict.get('key')}）：{ids} 内容互斥，需人工确认")
    return warnings


def build_context_package(
    engine,
    *,
    role: str,
    session_id: str = "",
    state_version: int = 0,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    token_counter: Optional[Callable[[str], int]] = None,
    tokenizer_name: str = "",
    focus: str = "",
    summaries: Sequence[str] = (),
    recent: Sequence[str] = (),
    zones: Sequence[str] = DEFAULT_ZONES,
) -> ContextPackage:
    """按四区规则组装一次上下文；不变量被破坏时降级为计划区并如实记录。"""
    if not str(role or "").strip():
        raise ValueError("role 不能为空")
    count = token_counter
    name = tokenizer_name
    if count is None:
        count, name = default_token_counter()
    elif not name:
        # 注入了计数器却没报名字：如实标注，别冒充 tiktoken。
        name = "injected:custom"
    assembler = ContextAssembler(engine, count)
    warnings: List[str] = []
    degraded = False
    try:
        result = assembler.assemble(
            summaries, recent, budget_tokens, focus=focus, zones=tuple(zones)
        )
        text = result.text
        sections = dict(result.zone_tokens)
        excluded = [
            {"id": node_id, "reason": reason}
            for node_id, reason in sorted(result.filtered.items())
        ]
        warnings = _conflict_warnings(result.provenance)
    except (PlanZoneOverflow, AuditError) as exc:
        # 组装不变量被破坏（计划区超预算 / 活跃约束缺席）：降级到计划区，
        # 把降级事实写进包，绝不静默当成"完整上下文"。
        degraded = True
        text = assembler.render_plan(focus=focus)
        audit = getattr(assembler, "_last_audit", None)
        excluded = (
            [{"id": node_id, "reason": reason}
             for node_id, reason in sorted(audit.filtered_ids().items())]
            if audit is not None
            else []
        )
        warnings = [f"{type(exc).__name__}: {exc}"]
        sections = {"plan": count(text)}
    included = sorted(set(_ID_PATTERN.findall(text)))

    return ContextPackage(
        context_id=str(uuid4()),
        session_id=session_id,
        role=str(role),
        state_version=int(state_version or 0),
        budget_tokens=int(budget_tokens),
        actual_tokens=int(count(text)),
        tokenizer=name,
        digest=digest_text(text),
        text=text,
        sections=sections,
        included_refs=included,
        excluded_refs=excluded,
        warnings=warnings,
        degraded=degraded,
        created_at=_now(),
    )
