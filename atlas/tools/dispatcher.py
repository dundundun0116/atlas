"""ToolDispatcher：把"调用一次工具"变成**可对账的一等事件**（设计文档 §4.8 / §6）。

调用序列（每一步都落库，崩溃点明确）：

    propose（落意图，同一幂等键只留一行）
      → dispatch（条件更新 claim；两个 worker 不会同时发）
      → 调工具
      → settle（settled / failed_determinate / failed_partial / indeterminate）

铁律：**`indeterminate` 之后绝不盲目重发**。结果未知时只有三条路——
用幂等键向工具查询、读回目标对象比对指纹、或者升级给人（`needs_human`）；
直接再发一次就可能造成重复副作用。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from atlas.tools.calls import (
    EffectClass,
    Outcome,
    ReconcileDecision,
    ToolCallStatus,
    ToolSpec,
)

#: 这些工具错误说明"请求压根没被受理"，可以安全重试。
_DETERMINATE_ERRORS = ("invalid_request", "no_evidence", "budget_exhausted")

#: 这些说明"动作部分生效"——不能重试，只能补偿或人工。
_PARTIAL_ERRORS = ("partial", "partial_apply", "partially_applied")

#: 这些说明"已经发出去了，结果不知道"——必须走对账，不能重发。
_UNKNOWN_ERRORS = ("timeout", "upstream_error", "unknown_commit")


@dataclass
class CallOutcome:
    """一次工具调用的结论（调用方只看这个）。"""

    status: ToolCallStatus
    outcome: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    reused: bool = False        # 命中了已有调用（幂等去重），这次没有真的发请求
    reconciled: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ToolCallStatus.SETTLED and self.outcome in (
            Outcome.APPLIED.value, "")

    @property
    def needs_human(self) -> bool:
        return self.status is ToolCallStatus.INDETERMINATE

    @property
    def retryable(self) -> bool:
        return self.status is ToolCallStatus.FAILED_DETERMINATE

    @property
    def needs_compensation(self) -> bool:
        """部分生效：只能补偿，不能重试。"""
        return self.status is ToolCallStatus.FAILED_PARTIAL

    def to_step_status(self) -> str:
        """映射到 Step 执行器要的状态字符串（runner 再按判据继续处理）。"""
        if self.ok:
            return "succeeded"
        if self.needs_human:
            return "indeterminate"
        return "failed"


class ToolDispatcher:
    """一个 Step 内的工具调用通道；依赖 repository 做持久化与对账。"""

    def __init__(self, repository, session_id: str, spec: ToolSpec, *,
                 plan_id: str = "", step_id: str = "", attempt_id: str = "",
                 scope: str = "", actor: str = "dispatcher"):
        self.repository = repository
        self.session_id = session_id
        self.spec = spec
        self.plan_id = plan_id
        self.step_id = step_id
        self.attempt_id = attempt_id
        self.scope = scope or f"{session_id}/{step_id}"
        self.actor = actor

    # ---------- 主流程 ----------

    def invoke(self, call_fn: Callable[[], Any], args: Dict[str, Any]) -> CallOutcome:
        """发起一次调用；`call_fn` 抛异常时按"是否可确定"分类落库。"""
        proposed = self.repository.propose_tool_call(
            self.session_id, tool_name=self.spec.name, args=args,
            step_id=self.step_id, plan_id=self.plan_id,
            attempt_id=self.attempt_id, effect_class=self.spec.effect_class.value,
            tool_version=self.spec.version, scope=self.scope, actor=self.actor,
        )
        call = proposed["tool_call"]
        tool_call_id = call["tool_call_id"]

        if not proposed["created"]:
            status = call["status"]
            if status in ("proposed", "failed_determinate"):
                # 意图已落但从未发出去（派发前崩溃 / 确定失败）：重派是安全的
                pass
            else:
                # 已派发或已定论：**不重发**，原样交回（未知的交给对账）
                return self._reuse(call)

        if not self.repository.dispatch_tool_call(
                self.session_id, tool_call_id, actor=self.actor):
            latest = self.repository.get_tool_call(self.session_id, tool_call_id)
            return self._reuse(latest)

        try:
            raw = call_fn()
        except Exception as exc:  # noqa: BLE001 - 分类比吞掉/重抛更重要
            return self._settle_error(tool_call_id, exc)
        return self._settle_success(tool_call_id, raw)

    def reconcile(self, tool_call_id: str,
                  *,
                  lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                  observe: Optional[Callable[[], Any]] = None,
                  expected: Any = None) -> CallOutcome:
        """对账：优先用幂等键查原结果，其次读回目标对象比对；实在判不了就留 indeterminate。"""
        call = self.repository.get_tool_call(self.session_id, tool_call_id)
        if call["status"] not in ("indeterminate", "reconciling"):
            return self._from_record(call, reconciled=call["status"] == "reconciled")

        if not self.repository.begin_reconcile(self.session_id, tool_call_id,
                                               actor=self.actor):
            latest = self.repository.get_tool_call(self.session_id, tool_call_id)
            return self._from_record(latest)

        if lookup is not None and self.spec.supports_lookup:
            found = lookup(call["idempotency_key"])
            if found is not None:
                record = self.repository.finish_reconcile(
                    self.session_id, tool_call_id, outcome=Outcome.APPLIED.value,
                    detail="按幂等键查到原结果", evidence={"found": True},
                    actor=self.actor,
                )
                record["result"] = dict(found)
                return self._from_record(record, reconciled=True)

        observed = observe() if observe is not None else None
        decision: ReconcileDecision = decide(observed, expected)
        record = self.repository.finish_reconcile(
            self.session_id, tool_call_id, outcome=decision.outcome.value
            if decision.outcome else Outcome.UNKNOWABLE.value,
            detail=decision.detail, evidence=decision.evidence, actor=self.actor,
        )
        return self._from_record(record, reconciled=record["status"] == "reconciled")

    # ---------- 内部 ----------

    def _reuse(self, call: Dict[str, Any]) -> CallOutcome:
        status = ToolCallStatus(call["status"])
        if status in (ToolCallStatus.DISPATCHED, ToolCallStatus.INDETERMINATE,
                      ToolCallStatus.RECONCILING):
            # 关键：可能已经发出去了，**不再发请求**，把决定权交回上层（对账或人工）
            return CallOutcome(
                status=ToolCallStatus.INDETERMINATE, tool_call_id=call["tool_call_id"],
                error={"kind": "unknown_commit",
                       "message": "上一次调用可能已发出且结果未知，必须先对账，不能重发"},
                reused=True,
            )
        return self._from_record(call, reconciled=status is ToolCallStatus.RECONCILED,
                                 reused=True)

    def _from_record(self, call: Dict[str, Any], *, reconciled: bool = False,
                     reused: bool = False) -> CallOutcome:
        return CallOutcome(
            status=ToolCallStatus(call["status"]),
            outcome=call.get("outcome") or "",
            result=dict(call.get("result") or {}),
            error=dict(call.get("error") or {}),
            tool_call_id=call["tool_call_id"],
            reused=reused,
            reconciled=reconciled,
        )

    def _settle_success(self, tool_call_id: str, raw: Any) -> CallOutcome:
        record = self.repository.settle_tool_call(
            self.session_id, tool_call_id, status=ToolCallStatus.SETTLED.value,
            outcome=Outcome.APPLIED.value,
            result=raw if isinstance(raw, dict) else {"value": raw},
            actor=self.actor,
        )
        return self._from_record(record)

    def _settle_error(self, tool_call_id: str, exc: BaseException) -> CallOutcome:
        kind = str(getattr(exc, "kind", "") or "exception")
        message = str(getattr(exc, "message", "") or exc)
        if kind in _PARTIAL_ERRORS:
            # 部分生效：既不能当成功，也不能直接重试（会重复那一半副作用）
            record = self.repository.settle_tool_call(
                self.session_id, tool_call_id,
                status=ToolCallStatus.FAILED_PARTIAL.value,
                outcome=Outcome.PARTIALLY_APPLIED.value,
                error={"kind": "partial", "message": message}, actor=self.actor,
            )
            return self._from_record(record)
        if kind in _DETERMINATE_ERRORS:
            record = self.repository.settle_tool_call(
                self.session_id, tool_call_id,
                status=ToolCallStatus.FAILED_DETERMINATE.value,
                outcome=Outcome.NOT_APPLIED.value,
                error={"kind": kind, "message": message}, actor=self.actor,
            )
            return self._from_record(record)
        # 已派发之后出错：默认按"结果未知"处理（§6：legacy adapter 一律 unknown_commit）
        record = self.repository.settle_tool_call(
            self.session_id, tool_call_id,
            status=ToolCallStatus.INDETERMINATE.value,
            outcome=Outcome.UNKNOWABLE.value,
            error={"kind": kind if kind in _UNKNOWN_ERRORS else "unknown_commit",
                   "message": message}, actor=self.actor,
        )
        return self._from_record(record)


def decide(observed: Any, expected: Any = None) -> ReconcileDecision:
    """读回观测 → 对账结论（薄封装，便于测试替换）。"""
    from atlas.tools.calls import decide_from_observation

    return decide_from_observation(observed, expected=expected)
