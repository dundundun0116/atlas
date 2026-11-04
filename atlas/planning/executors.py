"""研究流程每一步的执行器（把模型/工具接到 `PlanRunner` 上）。

纪律与 `Orchestrator` 一致：
- 协调全经共享图（`context_provider` 给的是四区组装出来的状态视图），不读对话历史；
- 模型调用走 `call_with_policy`（重试/超时预算真的生效）；
- 判据由 `PlanRunner` 用 `atlas.planning.criteria` 求值，执行器**不自判完成**——
  它只负责产出结构化 output，`approved` 这类字段是"给判据吃的原料"，不是"我说过了"。

与 `legacy_research_v1` 的差别：这里每一步是**独立 step**，输入由依赖 binding 解析而来
（`context["inputs"]`），所以某一步重跑时只依赖上游持久化的 output，不依赖内存里的对话。
"""

import json
from typing import Any, Callable, Dict, List, Optional

from atlas.context.extractor import parse_json_object
from atlas.environment.preconditions import (
    check_postcondition,
    check_precondition,
    observations_for,
    preconditions_from_payload,
)
from atlas.orchestration.policies import Budget, policy_for
from atlas.orchestration.orchestrator import (
    AUDITOR_PROMPT,
    REVISER_PROMPT,
    WORKER_PROMPT,
)
from atlas.planning.policies_call import call_model
from atlas.planning.runner import StepOutcome
from atlas.tools.calls import EffectClass, ToolSpec
from atlas.tools.compensation import (
    decide_after_call,
    requires_human_approval,
    run_compensation,
)
from atlas.tools.dispatcher import ToolDispatcher
from atlas.tools.protocol import ResearchRequest, ResearchResult, ResearchToolError


class ResearchStepExecutor:
    """按 step_id 分派：retrieve / draft / verify / repair / final_verify。"""

    def __init__(
        self,
        llm: Callable[[str], str],
        *,
        problem: str = "",
        research_tool: Any = None,
        context_provider: Optional[Callable[[str, str], str]] = None,
        session_id: str = "",
        subtask_id: str = "S1",
        budget_tokens: int = 4000,
        max_rounds: int = 2,
        budget: Optional[Budget] = None,
        repository: Any = None,
        write_tool: Any = None,
    ):
        self.llm = llm
        self.problem = problem
        self.research_tool = research_tool
        self.context_provider = context_provider
        self.session_id = session_id
        self.subtask_id = subtask_id
        self.budget_tokens = budget_tokens
        self.max_rounds = max_rounds
        self.budget = budget
        # 给了 repository 就把工具调用记成持久化 ToolCall（可对账、崩溃后不重发）
        self.repository = repository
        # 写工具（可选）：有它才跑 write / confirm 这两步
        self.write_tool = write_tool

    # ---------- 分派 ----------

    def __call__(self, step, context: Dict[str, Any]) -> StepOutcome:
        handler = getattr(self, f"_do_{step.step_id}", None)
        scope = dict(context.get("scope") or {})
        if handler is None:
            return StepOutcome(status="failed", error={
                "kind": "unknown_step", "message": f"没有执行器处理 step {step.step_id!r}"})
        inputs = dict(context.get("inputs") or {})
        if "__missing_inputs__" in inputs:
            return StepOutcome(status="failed", error={
                "kind": "missing_inputs",
                "message": f"缺少上游输入：{inputs['__missing_inputs__']}",
            })
        # Tool/LLM 适配器的通用纪律（§6）：派发前拒绝缺必需参数，不把空值传给下游。
        absent = [item.name for item in step.inputs
                  if item.required and item.source_kind == "step_output"
                  and item.name not in inputs]
        if absent:
            return StepOutcome(status="failed", error={
                "kind": "missing_inputs",
                "message": f"缺少必填输入：{absent}",
            })
        if step.step_id in ("retrieve", "write", "confirm"):
            return handler(step, inputs, scope)
        return handler(step, inputs)

    # ---------- 各步 ----------

    def _do_retrieve(self, step, inputs: Dict[str, Any], scope: Dict[str, Any] = None) -> StepOutcome:
        if self.research_tool is None:
            return StepOutcome(status="failed", error={
                "kind": "tool_unavailable", "message": "本部署没有配置 ResearchTool"})
        try:
            request = ResearchRequest(
                session_id=self.session_id or "session",
                subtask_id=self.subtask_id,
                query=self.problem,
                budget_tokens=self.budget_tokens,
                max_rounds=self.max_rounds,
            )
        except ValueError as exc:
            return StepOutcome(status="failed", error={
                "kind": "invalid_request", "message": str(exc)})

        def call() -> Dict[str, Any]:
            result = self.research_tool.research(request)
            if not isinstance(result, ResearchResult):
                result = ResearchResult.from_dict(result)
            return {
                "refs": [item.to_dict() for item in result.evidence],
                "answer": result.answer,
                "stop_reason": result.stop_reason,
                "research_trace_id": result.trace_id,
            }

        scope = dict(scope or {})
        if self.repository is None or not scope.get("session_id"):
            # 没有持久化上下文：直接调，工具失败是"确定失败"
            try:
                return StepOutcome(output=call())
            except ResearchToolError as exc:
                return StepOutcome(status="failed", error={
                    "kind": str(exc.kind), "message": str(exc.message),
                    "trace_id": exc.trace_id})
            except (TypeError, ValueError) as exc:
                return StepOutcome(status="failed", error={
                    "kind": "invalid_result", "message": str(exc)})

        spec = ToolSpec(
            name=str(step.tool_name or "compassrag"),
            version="1",
            effect_class=EffectClass.READ_ONLY,
            supports_idempotency=False,
            supports_lookup=False,
        )
        dispatcher = ToolDispatcher(
            self.repository, scope["session_id"], spec,
            plan_id=str(scope.get("plan_id") or ""),
            step_id=str(scope.get("step_id") or ""),
        )
        outcome = dispatcher.invoke(call, {
            "query": request.query,
            "subtask_id": request.subtask_id,
            "budget_tokens": request.budget_tokens,
            "max_rounds": request.max_rounds,
        })
        if outcome.ok:
            return StepOutcome(output=dict(outcome.result or {}))
        if outcome.needs_human:
            # 结果未知：冻结这一步等对账/人工，绝不自动重发
            return StepOutcome(status="indeterminate", error=dict(outcome.error or {}))
        return StepOutcome(status="failed", error=dict(outcome.error or {}))

    def _do_draft(self, step, inputs: Dict[str, Any]) -> StepOutcome:
        evidence = self._evidence_block(inputs.get("evidence"))
        prompt = WORKER_PROMPT.format(
            state_view=self._view("worker", self.problem),
            problem=f"{self.problem}\n\n{evidence}" if evidence else self.problem,
            route="先取证再作答；没有证据的部分要明确说明",
        )
        result = call_model(lambda: self.llm(prompt), "worker", budget=self.budget)
        if not result.ok:
            return StepOutcome(status="failed", error={
                "kind": "model_error", "message": result.error})
        return StepOutcome(output={"draft": result.value.strip()},
                           usage={"retries": max(0, result.attempts - 1)})

    def _do_verify(self, step, inputs: Dict[str, Any]) -> StepOutcome:
        payload = self._audit(inputs.get("draft", ""), role="verify",
                              evidence=inputs.get("evidence"))
        if payload is None:
            return StepOutcome(status="failed", error={
                "kind": "model_error", "message": "auditor 输出无法解析"})
        return StepOutcome(output=payload)

    def _do_repair(self, step, inputs: Dict[str, Any]) -> StepOutcome:
        draft = str(inputs.get("draft") or "")
        issues = str(inputs.get("issues") or "")
        # 判"需修订"时 auditor 必须给出 issues（Orchestrator 侧同一条纪律），
        # 所以"没有 issues"就等于没东西可修，原样交回即可。
        if not issues:
            # 没有可修的问题：原样交回草稿，仍然满足"产出 draft"的判据。
            return StepOutcome(output={"draft": draft, "revised": False})
        result = call_model(
            lambda: self.llm(REVISER_PROMPT.format(
                state_view=self._view("worker", self.problem),
                problem=self.problem, draft=draft, issues=issues,
            )),
            "worker", budget=self.budget,
        )
        if not result.ok:
            return StepOutcome(status="failed", error={
                "kind": "model_error", "message": result.error})
        return StepOutcome(output={"draft": result.value.strip(), "revised": True})

    def _do_final_verify(self, step, inputs: Dict[str, Any]) -> StepOutcome:
        payload = self._audit(inputs.get("draft", ""), role="final_verify",
                              evidence=inputs.get("evidence"))
        if payload is None:
            return StepOutcome(status="failed", error={
                "kind": "model_error", "message": "终检输出无法解析"})
        payload["approved"] = payload.get("verdict") == "pass"
        return StepOutcome(output=payload)

    # ---------- 写操作 ----------

    def _do_write(self, step, inputs: Dict[str, Any], scope: Dict[str, Any] = None) -> StepOutcome:
        """外部写入：派发 → 落 ToolCall → 部分生效时**当场补偿**。

        `applied_ok` 是给判据吃的原料：只有"整笔写入成功"才为 True；
        部分生效哪怕补偿成功也为 False——这一步确实没做成，只是世界被收拾干净了。
        """
        if self.write_tool is None:
            return StepOutcome(status="failed", error={
                "kind": "tool_unavailable", "message": "本部署没有配置写工具"})
        scope = dict(scope or {})
        target = str(inputs.get("target") or step.tool_args_template.get("target") or "")
        value = dict(inputs.get("value") or step.tool_args_template.get("value") or {})
        if not target or not value:
            return StepOutcome(status="failed", error={
                "kind": "invalid_request", "message": "write 步缺少 target/value"})
        if self.repository is None or not scope.get("session_id"):
            return StepOutcome(status="failed", error={
                "kind": "unavailable", "message": "写操作必须带持久化上下文（无法对账）"})

        spec = getattr(self.write_tool, "SPEC", None) or ToolSpec(
            name=str(step.tool_name or "demo_writer"),
            effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
            supports_idempotency=True, supports_lookup=True, supports_compensation=True,
        )
        # 派发**之前**的复查：计划是在某一刻对世界做的断言，等到真要派发时世界可能已变。
        # 不复查就只能等 confirm 读回才发现——那时候副作用已经发出去了。
        precheck = self._precheck(step, target)
        if precheck is not None and precheck.get("passed") is not True:
            blocked = "violated" if precheck.get("passed") is False else "unknown"
            return StepOutcome(
                status="failed" if blocked == "violated" else "needs_human",
                output={"target": target, "applied_ok": False, "compensated": False,
                        "precheck": precheck,
                        "env_changes": list(precheck.get("changes") or [])},
                error={"kind": f"precondition_{blocked}",
                       "message": precheck.get("reason", "")},
            )

        # 派发**之前**的闸门：副作用不可补偿的工具，出事之后只能靠人收拾，
        # 所以必须先让人点头，而不是"先写了再说"。`approved` 只能来自计划本身
        # （tool_args_template）或显式输入——执行器不替人拍板。
        approved = bool(inputs.get("approved")
                        or (step.tool_args_template or {}).get("approved"))
        if requires_human_approval(spec) and not approved:
            return StepOutcome(
                status="needs_human",
                output={"target": target, "value": value, "applied_ok": False,
                        "compensated": False, "approval_required": True},
                error={"kind": "approval_required",
                       "message": f"{spec.name} 的副作用不可补偿，派发前必须人工批准"},
            )
        dispatcher = ToolDispatcher(
            self.repository, scope["session_id"], spec,
            plan_id=str(scope.get("plan_id") or ""),
            step_id=str(scope.get("step_id") or ""),
        )
        # 真实网络工具要拿到幂等键才能放进请求头；内存替身不需要。
        if getattr(self.write_tool, "wants_idempotency_key", False):
            call_fn = (lambda key: self.write_tool.write(
                target, value, idempotency_key=key))  # noqa: E731
        else:
            call_fn = lambda: self.write_tool.write(target, value)  # noqa: E731
        outcome = dispatcher.invoke(call_fn, {"target": target, "value": value})
        output: Dict[str, Any] = {"target": target, "value": value,
                                  "applied_ok": False, "compensated": False}
        if outcome.ok:
            output["applied_ok"] = True
            return StepOutcome(output=output)

        if outcome.needs_compensation:
            # 协议：部分生效只能补偿；补偿本身也要落库、也要结算
            call = self.repository.get_tool_call(scope["session_id"], outcome.tool_call_id)
            decision = decide_after_call(call, spec,
                                         compensation_args={"key": target})
            if not decision.should_compensate:
                output["compensation_failed"] = decision.reason
                # 半成品还留在外部世界：这不是"失败"，是"必须有人来收"
                return StepOutcome(status="needs_human", output=output,
                                   error={"kind": "partial", "message": decision.reason})
            result = run_compensation(
                self.repository, scope["session_id"], call, spec,
                compensation_args=decision.compensation_args,
                perform=lambda args: self.write_tool.undo(args["key"]),
            )
            output["compensated"] = result["status"] == "settled"
            output["compensation_id"] = result["compensation_id"]
            output["compensated_ok"] = output["compensated"]
            if not output["compensated"]:
                return StepOutcome(status="needs_human", output=output,
                                   error={"kind": "compensation_failed",
                                          "message": str(result.get("error"))})
            return StepOutcome(status="failed", output=output,
                               error={"kind": "partial_compensated",
                                      "message": "部分写入已补偿，世界已复原"})
        if outcome.needs_human:
            # 结果未知：**不是失败**。失败会让计划继续往下走，而外部可能已经写进去了。
            output["tool_call_id"] = outcome.tool_call_id
            output["needs_reconcile"] = True
            return StepOutcome(status="indeterminate", output=output,
                               error=dict(outcome.error or {}))
        return StepOutcome(status="failed", output=output,
                           error=dict(outcome.error or {}))

    def _precheck(self, step, target: str) -> Optional[Dict[str, Any]]:
        """派发前复查外部世界；没有前置条件、或工具读不回，就返回 None（不拦）。

        三态语义与 `check_precondition` 一致：满足才放行；不满足是**确定失败**；
        观察过期/读不回是**判不了**，转人工——绝不能当成满足。
        """
        spec_conditions = preconditions_from_payload(step.tool_args_template)
        if not spec_conditions:
            return None
        observe = getattr(self.write_tool, "observe", None)
        if observe is None:
            return None
        try:
            observed = observe(target)
        except Exception as exc:  # noqa: BLE001 - 读不回就是判不了
            return {"passed": None, "observe_needed": True,
                    "reason": f"读回失败，无法复查前置条件：{exc}"}
        snapshot = observations_for({target: observed})
        failures = []
        for condition in spec_conditions:
            result = check_precondition(condition, snapshot, ttl_s=0)
            if result.passed is not True:
                failures.append({"condition": condition.to_dict(),
                                 "result": result.to_dict()})
        observed_fp = snapshot.get(target).fingerprint if snapshot.get(target) else ""
        if not failures:
            return {"passed": True, "reason": "前置条件满足",
                    "observed_fingerprint": observed_fp}
        passed = False if any(item["result"]["passed"] is False for item in failures) \
            else None
        # 记录"世界变成了什么样"：重规划要能解释触发信号
        changes = [{
            "target": target,
            "kind": "updated" if observed is not None else "created",
            "before": failures[0]["condition"].get("fingerprint") or "",
            "after": observed_fp,
        }]
        return {"passed": passed, "failures": failures,
                "reason": failures[0]["result"]["reason"],
                "observed_fingerprint": observed_fp, "changes": changes}

    def _do_confirm(self, step, inputs: Dict[str, Any], scope: Dict[str, Any] = None) -> StepOutcome:
        """读回目标对象：确认外部世界真的变了，而不是相信 write 的返回值。"""
        if self.write_tool is None:
            return StepOutcome(status="failed", error={
                "kind": "tool_unavailable", "message": "本部署没有配置写工具"})
        target = str(inputs.get("target") or step.tool_args_template.get("target") or "")
        if not target:
            return StepOutcome(status="failed", error={
                "kind": "invalid_request", "message": "confirm 步缺少 target"})
        expected = inputs.get("expected") or (step.tool_args_template or {}).get("expect")
        try:
            observed = self.write_tool.observe(target)
        except Exception as exc:  # noqa: BLE001 - 读不回 ≠ 没写成
            # 读回失败（超时/5xx）不能当成"确认失败"：那会把一次网络抖动
            # 读成"外部世界没写进去"，接着重跑就变成重复副作用。
            return StepOutcome(
                status="indeterminate",
                output={"target": target, "observed": None, "confirmed": None},
                error={"kind": getattr(exc, "kind", "unknown_commit"),
                       "message": f"读回失败，无法确认外部状态：{exc}"},
            )
        if observed is None:
            return StepOutcome(output={"target": target, "observed": None,
                                       "confirmed": False,
                                       "reason": "读回为空：外部对象不存在"})
        # 后置条件：读回必须**包含**我们写进去的字段（子集比对）。
        # 逐字段相等会把服务端补的 updated_at/version 当成失败——那是假阴性。
        post = check_postcondition(expected, observed) if isinstance(expected, dict) \
            else None
        confirmed = True if post is None else bool(post.confirmed)
        output = {
            "target": target,
            "observed": dict(observed),
            "confirmed": confirmed,
        }
        if post is not None:
            output["postcondition"] = post.to_dict()
            if not confirmed:
                output["reason"] = post.reason
        return StepOutcome(output=output)

    # ---------- 内部 ----------

    def _audit(self, draft: str, *, role: str,
               evidence: Any = None) -> Optional[Dict[str, Any]]:
        """核验草稿；`evidence` 必须一起给。

        为什么：核验者只拿到草稿、看不到草稿依据的检索结果时，它唯一能说的就是
        "这句话在共享状态图里找不到出处"——于是会把**正确答案**判成无证据，
        再让修订步把答案删掉。这不是模型的问题，是判据缺了输入。
        """
        view = self._view("auditor", self.problem)
        block = self._evidence_block(evidence)
        if block:
            view = f"{view}\n\n{block}".strip()
        result = call_model(
            lambda: self.llm(AUDITOR_PROMPT.format(
                state_view=view,
                problem=self.problem, draft=draft,
            )),
            "auditor", budget=self.budget,
        )
        if not result.ok:
            return None
        payload = parse_json_object(result.value)
        if payload is None:
            return None
        verdict = str(payload.get("verdict") or "").strip().lower()
        normalised = {"ok": "pass", "approve": "pass", "revise": "repair_required",
                      "retry": "repair_required", "blocked": "fatal"}.get(verdict, verdict)
        return {
            "verdict": normalised or "unknown",
            "raw_verdict": verdict,
            "issues": str(payload.get("issues") or "").strip(),
            "refs": [str(item) for item in (payload.get("refs") or [])],
            "draft_len": len(draft),
        }

    def _view(self, role: str, focus: str) -> str:
        if self.context_provider is None:
            return ""
        try:
            return str(self.context_provider(role, focus) or "")
        except Exception:  # 上下文组装失败不该让整步崩掉，但也别假装有内容
            return ""

    @staticmethod
    def _evidence_block(refs: Any) -> str:
        if not refs:
            return ""
        lines = ["[RETRIEVED EVIDENCE]"]
        for index, item in enumerate(refs, start=1):
            if isinstance(item, dict):
                source = item.get("source") or item.get("chunk_id") or "?"
                quote = (item.get("quote") or "").strip()
                lines.append(f"({index}) {source} — {quote}" if quote else f"({index}) {source}")
            else:
                lines.append(f"({index}) {item}")
        return "\n".join(lines)
