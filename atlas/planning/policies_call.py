"""模型调用的统一入口：按角色策略重试，并把消耗计进同一个 Budget。

`PlanRunner` 的每一步都要走这里——否则"重试预算"在 DAG 路径上又会变成死代码
（这正是 `legacy_research_v1` 之前被审计出来的问题）。
"""

from typing import Any, Callable, Optional

from atlas.orchestration.policies import (
    Budget,
    CallResult,
    RetryPolicy,
    call_with_policy,
    policy_for,
)


def call_model(
    fn: Callable[[], str],
    role: str,
    *,
    budget: Optional[Budget] = None,
    policy: Optional[RetryPolicy] = None,
) -> CallResult:
    """按角色策略调一次模型；成功后计一轮预算（token 由调用方按实提供，缺省不编）。"""
    resolved = policy or policy_for("worker" if role.startswith("worker") else role)
    result = call_with_policy(fn, resolved)
    if budget is not None and result.ok:
        budget.charge(rounds=1)
    return result
