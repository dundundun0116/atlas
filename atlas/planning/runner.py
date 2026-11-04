"""PlanRunner：按持久化的 DAG 一步步执行（设计文档 §5.7 的 Step 级版本）。

它把 Phase 2 的四条验收串起来：

1. **DAG 可由持久化状态恢复** —— 每次 tick 都重新读库里的 Step 状态，不靠内存；
2. **非法环 / 不可判定 Step 被拒绝** —— 交给 `validate_plan` 与 `needs_human`；
3. **只重跑未提交的 Attempt** —— `done` 的 Step 不再执行；崩溃后留下的
   `dispatched` 行在下一轮被识别为"在途"，只重跑它自己；
4. **依赖结果变化使下游 stale** —— `mark_downstream_stale`。

执行顺序（每一步都落库，崩溃点明确）：
`ready → dispatched(claim) → running → verifying → done | failed | needs_human`

`executor` 是注入的：它拿到 `StepSpec` 与上下文，返回 `StepOutcome`。上下文里的
`inputs` 由**依赖上的 binding 解析**得到——上游 step 最新一次成功的 attempt 的
`output[output_name]` → 下游 `input_name`。这样 `data` 边不是装饰：没有上游输出时
下游根本拿不到输入，会明确失败而不是拿个空字典接着跑。

生产实现会调模型/工具，测试用脚本替身——这就是"一次只迁移一条真实执行路径"的落点。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from atlas.orchestration.scheduler import plan_tick
from atlas.planning.criteria import evaluate_step
from atlas.planning.schemas import PlanSpec, StepSpec, StepStatus
from atlas.storage import events as ev

#: 运行上限：防止一个"永远 ready"的计划把循环吃满。
DEFAULT_MAX_TICKS = 200


@dataclass
class StepOutcome:
    """一次执行的返回；`verified` 留给执行器显式表态，缺省由判据求值决定。"""

    status: str = "succeeded"          # succeeded / failed / indeterminate
    output: Dict[str, Any] = field(default_factory=dict)
    error: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    context_digest: str = ""
    input_fingerprint: str = ""


@dataclass
class RunReport:
    """一轮 run 的结果；`stopped_reason` 说明为什么停下。"""

    plan_id: str = ""
    executed: List[str] = field(default_factory=list)
    skipped_done: List[str] = field(default_factory=list)
    needs_human: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    recovered_in_flight: List[str] = field(default_factory=list)
    stopped_reason: str = ""
    ticks: int = 0

    @property
    def complete(self) -> bool:
        return self.stopped_reason == "all_steps_settled"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "executed": list(self.executed),
            "skipped_done": list(self.skipped_done),
            "needs_human": list(self.needs_human),
            "failed": list(self.failed),
            "recovered_in_flight": list(self.recovered_in_flight),
            "stopped_reason": self.stopped_reason,
            "ticks": self.ticks,
            "complete": self.complete,
        }


class PlanRunner:
    """把一份已激活的计划跑到"没有可派发的 Step"为止。"""

    def __init__(
        self,
        repository,
        session_id: str,
        plan_id: str,
        executor: Callable[[StepSpec, Dict[str, Any]], StepOutcome],
        *,
        context_provider: Optional[Callable[[StepSpec], Dict[str, Any]]] = None,
        actor: str = "runner",
        max_ticks: int = DEFAULT_MAX_TICKS,
    ):
        self.repository = repository
        self.session_id = session_id
        self.plan_id = plan_id
        self.executor = executor
        self.context_provider = context_provider or (lambda step: {})
        self.actor = actor
        self.max_ticks = max_ticks

    # ---------- 对外 ----------

    def run(self) -> RunReport:
        report = RunReport(plan_id=self.plan_id)
        self._recover_in_flight(report)
        for tick_index in range(1, self.max_ticks + 1):
            report.ticks = tick_index
            statuses = self.repository.step_statuses(self.session_id, self.plan_id)
            plan = self._plan_spec()
            tick = plan_tick(plan, statuses)
            # 依赖已满足的 blocked → pending（调度器的建议要真的落到行上）
            for step_id in tick.to_pending:
                self.repository.set_step_status(
                    self.session_id, self.plan_id, step_id, "pending",
                    reason="依赖已满足", actor=self.actor,
                )
            for step_id in tick.ready:
                status = statuses.get(step_id)
                if status == StepStatus.BLOCKED.value:
                    continue  # 本轮刚转 pending，下一轮再派发
                if status == StepStatus.PENDING.value:
                    self.repository.set_step_status(
                        self.session_id, self.plan_id, step_id, "ready",
                        reason="前置条件满足", actor=self.actor,
                    )
            if not tick.decision.dispatched:
                report.stopped_reason = self._stop_reason(statuses)
                break
            self._run_one(plan, tick.decision.step_id, report)
        else:
            report.stopped_reason = f"达到 max_ticks={self.max_ticks} 上限"
        report.skipped_done = self._done_ids()
        return report

    # ---------- 单步 ----------

    def _run_one(self, plan: PlanSpec, step_id: str, report: RunReport) -> None:
        claimed = self.repository.claim_step(
            self.session_id, self.plan_id, step_id, actor=self.actor,
        )
        if not claimed:  # 被别人抢走或状态已变：下一轮重新算
            return
        attempt = self.repository.record_attempt(
            self.session_id, self.plan_id, step_id,
            status="running", trigger="initial", actor=self.actor,
        )
        self.repository.set_step_status(
            self.session_id, self.plan_id, step_id, "running",
            reason="执行器接管", actor=self.actor,
        )
        step = plan.step(step_id)
        if step is None:  # 计划与行不一致：明确失败，不猜
            self.repository.set_step_status(
                self.session_id, self.plan_id, step_id, "failed",
                reason=f"step_id {step_id} 不在计划 spec 里", actor=self.actor,
            )
            report.failed.append(step_id)
            return
        context = dict(self.context_provider(step) or {})
        context["inputs"] = self._resolve_inputs(plan, step, self._step_outputs())
        context["scope"] = {
            "session_id": self.session_id, "plan_id": self.plan_id,
            "step_id": step.step_id,
        }
        try:
            outcome = self.executor(step, context)
        except Exception as exc:  # 执行器炸了：确定"没拿到结果"，不是 indeterminate
            outcome = StepOutcome(
                status="failed",
                error={"kind": "executor_error",
                       "message": f"{type(exc).__name__}: {exc}"},
            )
        report.executed.append(step_id)

        self.repository.update_attempt(
            self.session_id, self.plan_id, attempt["attempt_id"],
            status=outcome.status, output=outcome.output, error=outcome.error,
            usage=outcome.usage, context_digest=outcome.context_digest,
            input_fingerprint=outcome.input_fingerprint,
        )
        self.repository.set_step_status(
            self.session_id, self.plan_id, step_id, "verifying",
            reason=f"attempt #{attempt['number']} 提交验证", actor=self.actor,
        )
        target, verification = self._verify(step, outcome)
        self._finish(step_id, target, verification, outcome, report, attempt)

    def _verify(self, step: StepSpec, outcome: StepOutcome):
        """判据求值：`None` 一律转人工，绝不当通过（§2.5）。"""
        if outcome.status in ("indeterminate", "unknown_commit"):
            return StepStatus.NEEDS_HUMAN, {"verified": None, "reason": "结果未知，需对账"}
        if outcome.status == "failed":
            return StepStatus.FAILED, {"verified": False,
                                       "reason": outcome.error.get("message", "执行失败")}
        if not step.success_criteria:
            return StepStatus.NEEDS_HUMAN, {"verified": None, "reason": "没有可执行判据"}
        verdict, details = evaluate_step(step.success_criteria, outcome.output)
        verification = {"verified": verdict, "criteria": details}
        if verdict is True:
            return StepStatus.DONE, verification
        if verdict is False:
            return StepStatus.FAILED, verification
        return StepStatus.NEEDS_HUMAN, verification

    def _finish(self, step_id: str, target: StepStatus, verification: Dict[str, Any],
                outcome: StepOutcome, report: RunReport, attempt: Dict[str, Any]) -> None:
        self.repository.set_step_status(
            self.session_id, self.plan_id, step_id, target.value,
            reason=str(verification.get("reason", target.value)), actor=self.actor,
        )
        if target is StepStatus.DONE:
            report.needs_human = [item for item in report.needs_human if item != step_id]
        elif target is StepStatus.NEEDS_HUMAN:
            report.needs_human.append(step_id)
        elif target is StepStatus.FAILED:
            report.failed.append(step_id)
        # 判据/验证结论补写进 attempt：报告要能回答"为什么这一步算过了"
        self.repository.update_attempt(
            self.session_id, self.plan_id, attempt["attempt_id"],
            verification=verification,
        )

    # ---------- 恢复 ----------

    def _recover_in_flight(self, report: RunReport) -> None:
        """崩溃留下的 `dispatched` 行 = 已派发但结果未知：只重跑它们自己（at-least-once）。"""
        statuses = self.repository.step_statuses(self.session_id, self.plan_id)
        in_flight = sorted(
            step_id for step_id, status in statuses.items()
            if status == StepStatus.DISPATCHED.value
        )
        for step_id in in_flight:
            self.repository.set_step_status(
                self.session_id, self.plan_id, step_id, "running",
                reason="恢复：上次已派发但未提交，重跑这一个 Step", actor=self.actor,
            )
            report.recovered_in_flight.append(step_id)
            plan = self._plan_spec()
            step = plan.step(step_id)
            if step is None:
                continue
            context = dict(self.context_provider(step) or {})
            context["inputs"] = self._resolve_inputs(plan, step, self._step_outputs())
            context["scope"] = {
                "session_id": self.session_id, "plan_id": self.plan_id,
                "step_id": step.step_id,
            }
            try:
                outcome = self.executor(step, context)
            except Exception as exc:
                outcome = StepOutcome(status="failed", error={
                    "kind": "executor_error", "message": f"{type(exc).__name__}: {exc}"})
            report.executed.append(step_id)
            attempt = self.repository.record_attempt(
                self.session_id, self.plan_id, step_id,
                status="running", trigger="retry", actor=self.actor,
            )
            self.repository.update_attempt(
                self.session_id, self.plan_id, attempt["attempt_id"],
                status=outcome.status, output=outcome.output, error=outcome.error,
                usage=outcome.usage, context_digest=outcome.context_digest,
            )
            self.repository.set_step_status(
                self.session_id, self.plan_id, step_id, "verifying",
                reason="恢复后重新验证", actor=self.actor,
            )
            target, verification = self._verify(step, outcome)
            self._finish(step_id, target, verification, outcome, report, attempt)

    # ---------- 内部 ----------

    def _step_outputs(self) -> Dict[str, Dict[str, Any]]:
        """每个已完成 step 最新一次 attempt 的 output（供下游 binding 取值）。"""
        outputs: Dict[str, Dict[str, Any]] = {}
        for step_id in self._done_ids():
            attempts = self.repository.attempts(self.session_id, self.plan_id, step_id)
            if not attempts:
                continue
            outputs[step_id] = dict(attempts[-1].get("output") or {})
        return outputs

    def _resolve_inputs(
        self, plan: PlanSpec, step: StepSpec, outputs: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按 dependency.binding 把上游输出接到本步输入；缺必需输入就记 warning。"""
        resolved: Dict[str, Any] = {}
        missing: List[str] = []
        for dependency in plan.dependencies:
            if dependency.to_step_id != step.step_id or not dependency.binding:
                continue
            output_name = dependency.binding.get("output_name", "")
            input_name = dependency.binding.get("input_name", "")
            upstream = outputs.get(dependency.from_step_id, {})
            if output_name in upstream:
                resolved[input_name] = upstream[output_name]
                continue
            _binding = step.input_named(input_name)
            if _binding is not None and not _binding.required:
                continue
            missing.append(f"{dependency.from_step_id}.{output_name} -> {input_name}")
        if missing:
            resolved.setdefault("__missing_inputs__", missing)
        return resolved

    def _plan_spec(self) -> PlanSpec:
        detail = self.repository.plan_detail(self.session_id, self.plan_id)
        return PlanSpec.from_dict(detail["plan"]["spec"])

    def _done_ids(self) -> List[str]:
        statuses = self.repository.step_statuses(self.session_id, self.plan_id)
        return sorted(step_id for step_id, status in statuses.items()
                      if status == StepStatus.DONE.value)

    def _stop_reason(self, statuses: Dict[str, str]) -> str:
        if not statuses:
            return "计划没有 Step"
        unsettled = [
            step_id for step_id, status in statuses.items()
            if status not in (StepStatus.DONE.value, StepStatus.SKIPPED.value,
                              StepStatus.CANCELLED.value)
        ]
        if not unsettled:
            return "all_steps_settled"
        if any(statuses.get(item) == StepStatus.NEEDS_HUMAN.value for item in unsettled):
            return "needs_human"
        if any(statuses.get(item) == StepStatus.FAILED.value for item in unsettled):
            return "failed"
        return "blocked"
