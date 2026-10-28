"""角色调用策略：request id、超时、有限重试、fallback，以及 Auditor 输出校验。

纪律（计划阶段 5）：
- 重试只针对**模型调用**本身（空输出、超时、连接错误）；
- 模型调用成功之后才提交状态操作，所以重试不会重复写入图；
- 不可重试错误（参数非法等）立即返回，不浪费额度；
- Auditor 的结论只能是 pass / repair_required / fatal，且必须引用状态节点或证据。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Sequence
from uuid import uuid4

#: Auditor 允许的三种结论。
AUDIT_OUTCOMES = ("pass", "repair_required", "fatal")

#: LLM 常见写法 → 规范结论。
_AUDIT_ALIASES = {
    "pass": "pass",
    "ok": "pass",
    "approve": "pass",
    "repair_required": "repair_required",
    "revise": "repair_required",
    "retry": "repair_required",
    "fatal": "fatal",
    "blocked": "fatal",
}


@dataclass(frozen=True)
class RetryPolicy:
    """一次角色调用的预算：次数、单次超时、退避。"""

    attempts: int = 2
    timeout_s: float = 30.0
    backoff_s: float = 0.0


@dataclass
class CallResult:
    ok: bool
    value: str = ""
    attempts: int = 0
    request_id: str = ""
    error: str = ""
    fallback_used: bool = False


#: 角色默认预算：解题的 Worker 给更多次；机械性 JSON 调用（Planner/Auditor）少而快。
ROLE_POLICIES = {
    "planner": RetryPolicy(attempts=2, timeout_s=60.0),
    "worker": RetryPolicy(attempts=3, timeout_s=180.0),
    "auditor": RetryPolicy(attempts=2, timeout_s=60.0),
}


def call_with_policy(
    fn: Callable[[], str],
    policy: RetryPolicy,
    *,
    request_id: Optional[str] = None,
    fallback: Optional[str] = None,
    retry_on_empty: bool = True,
) -> CallResult:
    """按预算调用模型；空输出与超时重试，其余异常立即失败。"""
    rid = request_id or f"req-{uuid4().hex[:12]}"
    last_error = ""
    attempts = 0
    for attempt in range(1, max(1, policy.attempts) + 1):
        attempts = attempt
        try:
            value = _call_with_timeout(fn, policy.timeout_s)
        except TimeoutError:
            last_error = f"timeout after {policy.timeout_s}s"
        except Exception as exc:  # 不可重试：模型/工具参数错误等
            return CallResult(
                ok=False,
                value=fallback or "",
                attempts=attempts,
                request_id=rid,
                error=f"{type(exc).__name__}: {exc}",
                fallback_used=fallback is not None,
            )
        else:
            if retry_on_empty and not str(value or "").strip():
                last_error = "empty model response"
            else:
                return CallResult(ok=True, value=str(value), attempts=attempts, request_id=rid)
        if policy.backoff_s and attempt < policy.attempts:
            time.sleep(policy.backoff_s)
    return CallResult(
        ok=False,
        value=fallback or "",
        attempts=attempts,
        request_id=rid,
        error=last_error,
        fallback_used=fallback is not None,
    )


def _call_with_timeout(fn: Callable[[], str], timeout_s: float) -> str:
    if not timeout_s or timeout_s <= 0:
        return fn()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout_s)
    except TimeoutError:
        raise
    finally:
        pool.shutdown(wait=False)


def normalize_audit_outcome(verdict: str) -> str:
    """把 LLM 的 verdict 归一成三种结论之一；认不出就报错，不猜。"""
    key = str(verdict or "").strip().lower()
    if key not in _AUDIT_ALIASES:
        raise ValueError(f"未知 Auditor 结论：{verdict!r}（可选 {sorted(_AUDIT_ALIASES)}）")
    return _AUDIT_ALIASES[key]


def validate_audit(outcome: str, references: Sequence[str] = ()) -> str:
    """结论必须在三态内，且必须引用状态节点或证据（防"空口核验"）。"""
    if outcome not in AUDIT_OUTCOMES:
        raise ValueError(f"Auditor 结论必须是 {AUDIT_OUTCOMES}，收到 {outcome!r}")
    refs = [r for r in references if str(r or "").strip()]
    if not refs:
        raise ValueError(f"Auditor 结论 {outcome} 必须引用状态节点或证据")
    return outcome


def policy_for(role: str) -> RetryPolicy:
    return ROLE_POLICIES.get(role, RetryPolicy())
