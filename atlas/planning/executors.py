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
from atlas.orchestration.policies import Budget, policy_for
from atlas.orchestration.orchestrator import (
    AUDITOR_PROMPT,
    REVISER_PROMPT,
    WORKER_PROMPT,
)
from atlas.planning.policies_call import call_model
from atlas.planning.runner import StepOutcome
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

    # ---------- 分派 ----------

    def __call__(self, step, context: Dict[str, Any]) -> StepOutcome:
        handler = getattr(self, f"_do_{step.step_id}", None)
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
        return handler(step, inputs)

    # ---------- 各步 ----------

    def _do_retrieve(self, step, inputs: Dict[str, Any]) -> StepOutcome:
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
        try:
            result = self.research_tool.research(request)
        except ResearchToolError as exc:
            # 工具失败是"确定没拿到结果"，不是 unknown：可以重试/重规划。
            return StepOutcome(status="failed", error={
                "kind": str(exc.kind), "message": str(exc.message),
                "trace_id": exc.trace_id})
        if not isinstance(result, ResearchResult):
            try:
                result = ResearchResult.from_dict(result)
            except (TypeError, ValueError) as exc:
                return StepOutcome(status="failed", error={
                    "kind": "invalid_result", "message": str(exc)})
        return StepOutcome(output={
            "refs": [item.to_dict() for item in result.evidence],
            "answer": result.answer,
            "stop_reason": result.stop_reason,
            "research_trace_id": result.trace_id,
        })

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
        payload = self._audit(inputs.get("draft", ""), role="verify")
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
        payload = self._audit(inputs.get("draft", ""), role="final_verify")
        if payload is None:
            return StepOutcome(status="failed", error={
                "kind": "model_error", "message": "终检输出无法解析"})
        payload["approved"] = payload.get("verdict") == "pass"
        return StepOutcome(output=payload)

    # ---------- 内部 ----------

    def _audit(self, draft: str, *, role: str) -> Optional[Dict[str, Any]]:
        result = call_model(
            lambda: self.llm(AUDITOR_PROMPT.format(
                state_view=self._view("auditor", self.problem),
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
