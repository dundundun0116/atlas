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
from typing import Any, Callable, Dict, Iterable, Optional, Sequence
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


class BudgetExhausted(RuntimeError):
    """统一预算触顶：`reason` 说明是轮数、token、延迟、费用还是修订次数。"""

    def __init__(self, reason: str):
        super().__init__(f"budget_exhausted: {reason}")
        self.reason = reason


@dataclass
class Budget:
    """一次任务的统一预算：修订次数、轮数、token、延迟、费用。

    只有"运行时真的知道"的量才计费：
    - 轮数与延迟由编排器实测；
    - token/费用由调用方提供 usage（没有就记为 0，不估算成假数字）。
    """

    max_repairs: int = 1
    max_rounds: int = 8
    max_tokens: int = 20000
    max_latency_ms: float = 600000.0
    max_cost_usd: float = 0.5
    rounds: int = 0
    repairs: int = 0
    tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0

    def charge(self, *, rounds: int = 0, tokens: int = 0,
               latency_ms: float = 0.0, cost_usd: float = 0.0) -> None:
        self.rounds += int(rounds)
        self.tokens += int(tokens)
        self.latency_ms += float(latency_ms)
        self.cost_usd += float(cost_usd)
        self.check()

    def spend_repair(self) -> None:
        self.repairs += 1

    def exhausted(self) -> Optional[str]:
        if self.rounds > self.max_rounds:
            return f"rounds {self.rounds} > {self.max_rounds}"
        if self.tokens > self.max_tokens:
            return f"tokens {self.tokens} > {self.max_tokens}"
        if self.latency_ms > self.max_latency_ms:
            return f"latency {self.latency_ms:.0f}ms > {self.max_latency_ms:.0f}ms"
        if self.cost_usd > self.max_cost_usd:
            return f"cost {self.cost_usd:.4f} > {self.max_cost_usd:.4f}"
        if self.repairs > self.max_repairs:
            return f"repairs {self.repairs} > {self.max_repairs}"
        return None

    def check(self) -> None:
        reason = self.exhausted()
        if reason:
            raise BudgetExhausted(reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds, "max_rounds": self.max_rounds,
            "tokens": self.tokens, "max_tokens": self.max_tokens,
            "latency_ms": round(self.latency_ms, 2), "max_latency_ms": self.max_latency_ms,
            "cost_usd": round(self.cost_usd, 6), "max_cost_usd": self.max_cost_usd,
            "repairs": self.repairs, "max_repairs": self.max_repairs,
        }


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


def validate_audit_refs(outcome: str, references: Sequence[str],
                        known_ids: Optional[Iterable[str]] = None) -> str:
    """结论引用必须指向**真实存在**的节点或证据（Task 2）。"""
    validate_audit(outcome, references)
    if known_ids is None:
        return outcome
    known = set(known_ids)
    unknown = [ref for ref in references if ref not in known]
    if unknown:
        raise ValueError(f"Auditor 引用了不存在的节点：{unknown}")
    return outcome
