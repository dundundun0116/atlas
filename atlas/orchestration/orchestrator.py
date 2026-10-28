"""多智能体编排：Planner → Worker → Auditor →（必要时）Worker 修订。

- 每个子任务内做多次内部 LLM 调用（不进环境），最后收敛为一个动作——适配 MemoryArena
  的"每子任务单动作"硬约束；
- 协调全经共享图：角色把结论/疑虑写入图（role_note），下一角色读图状态视图
  （state_view）而非对话历史；
- 固定三段式：Planner 定路线（Decision 节点）→ Worker 解题（Artifact 草稿）→
  Auditor 核验（Fact 疑虑）→ 仅当 Auditor 判"需修订"时 Worker 修订（作废草稿、记原因）。

LLM 与图客户端均注入（测试用替身；生产为 openai 客户端与 memory server HTTP）。
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from atlas.context.extractor import parse_json_object
from atlas.orchestration.policies import normalize_audit_outcome, validate_audit
from atlas.orchestration.state_machine import IllegalTransition, TaskStateMachine

PLANNER_PROMPT = """You are the PLANNER in a multi-agent math solving system. You coordinate with the
other roles ONLY through the shared state graph shown below (no direct messages).

[SHARED STATE GRAPH]
{state_view}

[PROBLEM]
{problem}

Decide the solution route: which established results and constraints matter, which method to use,
and what could go wrong.
Output strict JSON only:
{{"route": "<1-3 sentences: the solution route>", "risks": "<1-2 sentences, or empty>",
 "reason": "<why this route>"}}"""

WORKER_PROMPT = """You are the WORKER in a multi-agent math solving system. You coordinate with the
other roles ONLY through the shared state graph shown below.

[SHARED STATE GRAPH]
{state_view}

[PROBLEM]
{problem}

[PLANNER'S ROUTE]
{route}

Solve the problem following the route. Give concise reasoning and state the final answer clearly."""

AUDITOR_PROMPT = """You are the AUDITOR in a multi-agent math solving system. You coordinate with the
other roles ONLY through the shared state graph shown below.

[SHARED STATE GRAPH]
{state_view}

[PROBLEM]
{problem}

[DRAFT ANSWER]
{draft}

Check the draft against the active constraints and decisions in the graph, and check the math itself.
Output strict JSON only:
{{"verdict": "ok" or "revise" or "fatal", "issues": "<specific, actionable issues; empty when ok>",
 "refs": ["<node ids you relied on, e.g. C1/D2>"]}}"""

REVISER_PROMPT = """You are the WORKER revising your draft after the AUDITOR's review.

[SHARED STATE GRAPH]
{state_view}

[PROBLEM]
{problem}

[YOUR DRAFT]
{draft}

[AUDITOR'S ISSUES]
{issues}

Produce the corrected final answer with concise reasoning."""

WORKER_FALLBACK_PROMPT = """Solve the following problem. Give concise reasoning and state the final
answer clearly.

[PROBLEM]
{problem}"""


class OrchestrationError(RuntimeError):
    """编排无法完成（调用失败/输出不可用）；调用方按降级链处理。"""


@dataclass
class OrchestrationTrace:
    calls: List[str] = field(default_factory=list)
    route: str = ""
    draft: str = ""
    verdict: str = ""
    issues: str = ""
    final: str = ""


class Orchestrator:
    """固定三段式编排器；graph 需实现 state_view(role) 与 role_note(role, kind, content, reason, evidence)。

    `llm_no_think` 用于机械性 JSON 调用（Planner/Auditor）：这类调用不需要长推理，
    在支持关推理的端点（opencode go：`thinking.type=disabled`）上可快一个数量级；
    缺省时与 `llm` 相同（真实解题的 Worker/修订始终用 `llm`）。
    """

    def __init__(self, llm: Callable[[str], str], graph,
                 llm_no_think: Callable[[str], str] = None,
                 machine: Optional[TaskStateMachine] = None):
        self.llm = llm
        self.llm_no_think = llm_no_think or llm
        self.graph = graph
        # 可选：显式状态机。给了就按合法转移推进，任何失败都收敛到 FAILED 并留原因。
        self.machine = machine

    def solve(self, problem: str) -> Tuple[str, OrchestrationTrace]:
        try:
            return self._solve(problem)
        except OrchestrationError as e:
            self._transition("fail", str(e))
            raise

    def _transition(self, trigger: str, reason: str = "") -> None:
        """推进状态机；未注入状态机时是 no-op。"""
        if self.machine is None:
            return
        try:
            self.machine.apply(trigger, reason=reason)
        except IllegalTransition:
            if not self.machine.is_terminal:
                raise

    def _solve(self, problem: str) -> Tuple[str, OrchestrationTrace]:
        trace = OrchestrationTrace()

        # 1) Planner：定路线 → 写 Decision（actor=planner）
        plan = self._call_json(
            PLANNER_PROMPT.format(state_view=self.graph.state_view("planner"), problem=problem),
            "planner",
            llm=self.llm_no_think,
        )
        trace.calls.append("planner")
        route = str(plan.get("route") or "").strip()
        if not route:
            raise OrchestrationError("planner 未给出 route")
        trace.route = route
        self.graph.role_note(
            "planner", "decision", route,
            reason=str(plan.get("reason") or "").strip() or "planner 路线选择",
            evidence=str(plan.get("risks") or "").strip(),
        )
        self._transition("plan_ready", f"planner 路线：{route}")

        # 2) Worker：按路线解题 → 写 Artifact 草稿
        draft = self._call(
            WORKER_PROMPT.format(state_view=self.graph.state_view("worker"), problem=problem, route=route),
            "worker",
        ).strip()
        trace.calls.append("worker")
        if not draft:
            raise OrchestrationError("worker 无输出")
        trace.draft = draft
        self.graph.role_note("worker", "artifact", draft)
        self._transition("work_done", "worker 草稿完成")

        # 3) Auditor：核验草稿 → 问题写 Fact（actor=auditor）
        audit = self._call_json(
            AUDITOR_PROMPT.format(
                state_view=self.graph.state_view("auditor"), problem=problem, draft=draft
            ),
            "auditor",
            llm=self.llm_no_think,
        )
        trace.calls.append("auditor")
        verdict = str(audit.get("verdict") or "").strip().lower()
        issues = str(audit.get("issues") or "").strip()
        trace.verdict = verdict
        trace.issues = issues
        try:
            outcome = normalize_audit_outcome(verdict)
        except ValueError:
            outcome = "repair_required"  # 认不出的结论按"需修订"处理，不当作通过
        # 给了引用就按"结论必须引用节点或证据"校验；没给则保留宽松模式（阶段 7 收紧）
        refs = [str(r).strip() for r in (audit.get("refs") or []) if str(r).strip()]
        if refs:
            try:
                validate_audit(outcome, refs)
            except ValueError as e:
                raise OrchestrationError(f"auditor 结论不合法：{e}") from e
        if outcome == "fatal":
            self._transition("audit_fatal", issues or "auditor 判 fatal")
            raise OrchestrationError(f"auditor 判 fatal：{issues or '无误导原因'}")
        if outcome == "repair_required" and issues:
            self._transition("audit_repair", issues)
        elif outcome == "pass":
            self._transition("audit_pass", "auditor 核验通过")

        # 4) 仅当 Auditor 判"需修订"：Worker 修订一次（作废草稿、原因入图）
        if verdict == "revise" and issues:
            self.graph.role_note("auditor", "fact", f"草稿存在问题：{issues}", reason="auditor 核验")
            try:
                revised = self._call(
                    REVISER_PROMPT.format(
                        state_view=self.graph.state_view("worker"),
                        problem=problem,
                        draft=draft,
                        issues=issues,
                    ),
                    "worker_revise",
                ).strip()
            except OrchestrationError:
                revised = ""  # 修订失败：保留草稿（auditor 的疑虑已在图里）
                self._transition("repair_done", "修订失败，保留草稿")
                self._transition("audit_pass", "降级交付草稿")
            trace.calls.append("worker_revise")
            if revised:
                trace.final = revised
                self.graph.role_note("worker", "artifact_revise", revised, reason=issues)
                self._transition("repair_done", "修订稿完成")
                self._transition("audit_pass", "修订后交付")
                return revised, trace

        trace.final = draft
        return draft, trace

    # ---------- 内部 ----------

    def _call(self, prompt: str, role: str, llm: Callable[[str], str] = None) -> str:
        try:
            return (llm or self.llm)(prompt) or ""
        except Exception as e:
            raise OrchestrationError(f"{role} 调用失败：{type(e).__name__}: {e}") from e

    def _call_json(self, prompt: str, role: str, llm: Callable[[str], str] = None) -> dict:
        payload = parse_json_object(self._call(prompt, role, llm))
        if payload is None:
            raise OrchestrationError(f"{role} 输出无法解析为 JSON")
        return payload
