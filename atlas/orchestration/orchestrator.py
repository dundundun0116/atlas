"""多智能体编排：Planner → Worker → Auditor →（必要时）Worker 修订。

- 每个子任务内做多次内部 LLM 调用（不进环境），最后收敛为一个动作——适配 MemoryArena
  的"每子任务单动作"硬约束；
- 协调全经共享图：角色把结论/疑虑写入图（role_note），下一角色读图状态视图
  （state_view）而非对话历史；
- 固定三段式：Planner 定路线（Decision 节点）→ Worker 解题（Artifact 草稿）→
  Auditor 核验（Fact 疑虑）→ 仅当 Auditor 判"需修订"时 Worker 修订（作废草稿、记原因）。

LLM 与图客户端均注入（测试用替身；生产为 openai 客户端与 memory server HTTP）。
"""

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from atlas.context.extractor import parse_json_object
from atlas.observability.tracing import hash_input
from atlas.orchestration.policies import (
    Budget,
    BudgetExhausted,
    CallResult,
    call_with_policy,
    normalize_audit_outcome,
    policy_for,
    validate_audit_refs,
)
from atlas.tools.compassrag import describe_failure
from atlas.tools.protocol import ResearchRequest, ResearchResult, ResearchToolError
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
    # 阶段 A：研究工具调用记录（失败也可审计，不写进图）
    research_trace_id: str = ""
    research_rounds: int = 0
    research_stop_reason: str = ""
    research_confidence: float = 0.0
    research_error: Optional[dict] = None
    research_reused: bool = False
    fact_ids: List[str] = field(default_factory=list)
    # 每个模型调用都留一条可审计记录：上下文 digest + 重试次数 + 延迟。
    # 成功与失败都在里面——失败路径缺 trace 是之前审计出来的问题。
    call_events: List[Dict[str, Any]] = field(default_factory=list)
    context_digests: List[str] = field(default_factory=list)
    retries: int = 0


class Orchestrator:
    """固定三段式编排器；graph 需实现 state_view(role) 与 role_note(role, kind, content, reason, evidence)。

    `llm_no_think` 用于机械性 JSON 调用（Planner/Auditor）：这类调用不需要长推理，
    在支持关推理的端点（opencode go：`thinking.type=disabled`）上可快一个数量级；
    缺省时与 `llm` 相同（真实解题的 Worker/修订始终用 `llm`）。

    `transition`：把状态转移**交给调用方持久化**（例如 `runtime.advance_task`）。
    给了它就不用 `machine`——内存状态机推进后不落 checkpoint，进程重启就丢；
    状态唯一真源是 repository，所以转移必须走带持久化的那条路。
    `trace_sink`：每个模型/工具调用后回调一条 trace（成功失败都写）。
    """

    def __init__(self, llm: Callable[[str], str], graph,
                 llm_no_think: Callable[[str], str] = None,
                 machine: Optional[TaskStateMachine] = None,
                 research_tool=None,
                 research_budget_tokens: int = 4000,
                 research_max_rounds: int = 2,
                 budget: Optional[Budget] = None,
                 transition: Optional[Callable[[str, str], None]] = None,
                 trace_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
                 retry_policy: Optional[Callable[[str], Any]] = None,
                 run_id: str = ""):
        self.llm = llm
        self.llm_no_think = llm_no_think or llm
        self.graph = graph
        # 阶段 A：可选的 ResearchTool（CompassRAG）；不给就沿用纯 LLM 流程。
        self.research_tool = research_tool
        self.research_budget_tokens = research_budget_tokens
        self.research_max_rounds = research_max_rounds
        self._research_cache: dict = {}
        self._research_written: set = set()
        # 统一预算：轮数 / token / 延迟 / 费用（修订次数由状态机读同一个预算）
        self.budget = budget
        # 可选：显式状态机。给了就按合法转移推进，任何失败都收敛到 FAILED 并留原因。
        self.machine = machine
        # 更推荐的路径：转移由调用方持久化（machine 只用于内存单测）。
        self.transition = transition
        self.trace_sink = trace_sink
        self._policy_for = retry_policy or self._default_policy
        self.run_id = run_id
        self._trace = OrchestrationTrace()
        self._last_context_digest = ""
        self._last_context_tokens = 0
        self._state_version = 0

    @staticmethod
    def _default_policy(role: str):
        # 修订也是 Worker 的活：沿用 worker 的重试/超时预算。
        return policy_for("worker" if role.startswith("worker") else role)

    def solve(self, problem: str, *, session_id: str = "", subtask_id: str = "S1",
              research_query: str = "") -> Tuple[str, OrchestrationTrace]:
        self._trace = OrchestrationTrace()
        try:
            return self._solve(
                problem, session_id=session_id, subtask_id=subtask_id,
                research_query=research_query,
            )
        except BudgetExhausted as e:
            self._converge_failure(str(e))
            raise OrchestrationError(str(e)) from e
        except OrchestrationError as e:
            self._converge_failure(str(e))
            raise

    def _converge_failure(self, reason: str) -> None:
        """任何失败都收敛到 FAILED 并留原因；已经终态则不再重复转移。"""
        try:
            self._transition("fail", reason or "编排失败")
        except OrchestrationError:
            pass

    def _transition(self, trigger: str, reason: str = "") -> None:
        """推进状态：优先交给调用方的持久化转移，其次才是内存状态机。"""
        if self.transition is not None:
            try:
                self.transition(trigger, reason)
            except Exception as exc:  # 持久化转移失败（状态冲突/存储错误）不吞
                raise OrchestrationError(
                    f"状态转移 {trigger} 失败：{type(exc).__name__}: {exc}"
                ) from exc
            return
        if self.machine is None:
            return
        try:
            self.machine.apply(trigger, reason=reason)
        except IllegalTransition:
            if not self.machine.is_terminal:
                raise

    # ---------- 上下文与 trace ----------

    def _context(self, role: str, focus: str = "") -> str:
        """取角色看到的状态视图；顺带记住这次的 context digest（供 trace 下钻）。"""
        builder = getattr(self.graph, "context_package", None)
        if callable(builder):
            package = builder(role, focus=focus)
            self._last_context_digest = str(getattr(package, "digest", "") or "")
            self._last_context_tokens = int(getattr(package, "actual_tokens", 0) or 0)
            # trace 里的 state_version 用组包时的真实图版本，而不是开场那个数
            self._state_version = int(
                getattr(package, "state_version", 0) or self._state_version
            )
            self._trace.context_digests.append(self._last_context_digest)
            return str(getattr(package, "text", "") or "")
        # 老图客户端只有 state_view：保持兼容，但不假装有 digest。
        self._last_context_digest = ""
        self._last_context_tokens = 0
        return self.graph.state_view(role)

    def _emit_trace(self, *, role: str, prompt: str, result: Optional[CallResult],
                    latency_ms: float, outcome: str, error: str = "",
                    tool_name: str = "model", stop_reason: str = "",
                    context_digest: Optional[str] = None) -> None:
        """一次调用的审计事实（成功也写）。无 sink 时只留在 trace 对象里。"""
        digest = self._last_context_digest if context_digest is None else context_digest
        tokens = self._last_context_tokens if context_digest is None else 0
        event = {
            "trace_id": f"orch-{uuid4().hex[:12]}",
            "run_id": self.run_id,
            "agent_role": role,
            "tool_name": tool_name,
            "step_id": role,
            "input_hash": hash_input(prompt),
            "latency_ms": round(float(latency_ms), 2),
            "retries": max(0, (result.attempts - 1)) if result is not None else 0,
            "state_version": self._state_version,
            "outcome": outcome,
            "stop_reason": stop_reason,
            "error": error,
            # 上下文可复盘：digest 指向 context_packages 表里的那一条。
            "context_digest": digest,
            "context_tokens": tokens,
            "usage_known": False,  # 模型侧没有给 usage：不编造 token 数
        }
        self._trace.call_events.append(event)
        if self.trace_sink is not None:
            try:
                self.trace_sink(event)
            except Exception:  # 写 trace 失败不能反过来打断编排
                pass

    def _solve(self, problem: str, *, session_id: str = "", subtask_id: str = "S1",
               research_query: str = "") -> Tuple[str, OrchestrationTrace]:
        trace = self._trace
        self._state_version = len(self._known_ids() or ())

        # 1) Planner：定路线 → 写 Decision（actor=planner）
        started = time.perf_counter()
        plan = self._call_json(
            PLANNER_PROMPT.format(state_view=self._context("planner", problem), problem=problem),
            "planner",
            llm=self.llm_no_think,
        )
        self._charge(started)
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

        # 1.5) 研究工具（阶段 A）：取证据 → 写 Fact（含 provenance）→ 喂给 Worker
        evidence_block = self._research(
            trace, session_id, subtask_id, research_query or route or problem
        )
        worker_problem = problem if not evidence_block else f"{problem}\n\n{evidence_block}"

        # 2) Worker：按路线解题 → 写 Artifact 草稿
        started = time.perf_counter()
        draft = self._call(
            WORKER_PROMPT.format(state_view=self._context("worker", problem),
                                 problem=worker_problem, route=route),
            "worker",
        ).strip()
        self._charge(started)
        trace.calls.append("worker")
        if not draft:
            raise OrchestrationError("worker 无输出")
        trace.draft = draft
        self.graph.role_note("worker", "artifact", draft)
        self._transition("work_done", "worker 草稿完成")

        # 3) Auditor：核验草稿 → 问题写 Fact（actor=auditor）
        started = time.perf_counter()
        audit = self._call_json(
            AUDITOR_PROMPT.format(
                state_view=self._context("auditor", problem), problem=problem, draft=draft
            ),
            "auditor",
            llm=self.llm_no_think,
        )
        self._charge(started)
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
                validate_audit_refs(outcome, refs, self._known_ids())
            except ValueError as e:
                raise OrchestrationError(f"auditor 结论不合法：{e}") from e
        if outcome == "fatal":
            self._transition("audit_fatal", issues or "auditor 判 fatal")
            raise OrchestrationError(f"auditor 判 fatal：{issues or '未给出原因'}")
        if outcome == "repair_required" and not issues:
            # 判"需修订"却说不出问题：没有可执行判据，不允许当成通过（设计文档 §2.5）。
            # 之前这条路径会停在 AUDITING 不收敛，最后由调用方顺手 audit_pass。
            raise OrchestrationError(
                f"auditor 判需修订但未给出可执行问题（verdict={verdict!r}）"
            )
        if outcome == "repair_required":
            self._transition("audit_repair", issues)
        else:
            self._transition("audit_pass", "auditor 核验通过")

        # 4) 仅当 Auditor 判"需修订"：Worker 修订一次（作废草稿、原因入图）
        if outcome == "repair_required":
            self.graph.role_note("auditor", "fact", f"草稿存在问题：{issues}", reason="auditor 核验")
            try:
                revised = self._call(
                    REVISER_PROMPT.format(
                        state_view=self._context("worker", problem),
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

    # ---------- 研究工具 ----------

    def _research(self, trace, session_id, subtask_id, query) -> str:
        """调研究工具；成功则写 Fact（含 evidence provenance）并返回喂给 Worker 的文本。

        工具失败只进 trace，不写图、不改状态机 —— 已有状态不受影响。
        """
        if self.research_tool is None:
            return ""
        try:
            request = ResearchRequest(
                session_id=session_id or "session",
                subtask_id=subtask_id or "S1",
                query=str(query or "").strip(),
                budget_tokens=self.research_budget_tokens,
                max_rounds=self.research_max_rounds,
            )
        except ValueError as exc:
            trace.research_error = {"kind": "invalid_request", "message": str(exc), "trace_id": ""}
            return ""
        trace.research_trace_id = request.trace_id
        started = time.perf_counter()
        result = self._research_cache.get(request.idempotency_key)
        if result is None:
            try:
                result = self.research_tool.research(request)
            except ResearchToolError as exc:
                trace.research_error = describe_failure(exc)
                # 失败也尽量保留上游 trace（CompassRAG 侧可能已经跑了一半）
                trace.research_trace_id = exc.trace_id or trace.research_trace_id
                self._emit_trace(
                    role="research", prompt=request.query, result=None,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    outcome="error", error=str(exc.kind), tool_name="compassrag",
                    stop_reason=str(exc.kind), context_digest="",
                )
                return ""
            self._research_cache[request.idempotency_key] = result
        else:
            trace.research_reused = True
            started = time.perf_counter()
        if not isinstance(result, ResearchResult):  # 防止自定义工具返回裸 dict
            try:
                result = ResearchResult.from_dict(result)
            except (TypeError, ValueError) as exc:
                trace.research_error = {
                    "kind": "invalid_request", "message": str(exc), "trace_id": request.trace_id
                }
                self._emit_trace(
                    role="research", prompt=request.query, result=None,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    outcome="error", error=str(exc), tool_name="compassrag",
                    stop_reason="invalid_result", context_digest="",
                )
                return ""
        self._emit_trace(
            role="research", prompt=request.query, result=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            outcome="ok", tool_name="compassrag",
            stop_reason=str(result.stop_reason or ""), context_digest="",
        )
        # 记录上游（CompassRAG）自己的 trace id：Atlas 的 task trace 据此下钻，
        # 而不是只留下 Atlas 侧编的关联 id。
        trace.research_trace_id = result.trace_id or trace.research_trace_id
        trace.research_rounds = result.retrieval_rounds
        trace.research_stop_reason = result.stop_reason
        trace.research_confidence = result.confidence
        if not result.usable:
            trace.research_error = {
                "kind": "no_evidence",
                "message": "工具没有给出结论",
                "trace_id": result.trace_id or request.trace_id,
            }
            return ""
        if request.idempotency_key in self._research_written:
            # 同一幂等键：结论已经在图里，只复用文本，不重复写 Fact
            return self._format_evidence(result)
        note = self._role_note_with_provenance(
            "research",
            "fact",
            result.answer,
            reason=(
                f"检索 {result.retrieval_rounds} 轮，confidence={result.confidence:.2f}，"
                f"stop={result.stop_reason or 'unknown'}"
            ),
            evidence=result.evidence_json(),
            source=",".join(item.source for item in result.evidence[:3]),
            confidence=result.confidence,
            # 统一报告据此从 Fact 下钻到 CompassRAG 的单次检索轨迹
            meta={
                "research_trace_id": result.trace_id,
                "research_stop_reason": result.stop_reason,
                "research_rounds": result.retrieval_rounds,
                "research_confidence": result.confidence,
            },
        )
        self._research_written.add(request.idempotency_key)
        node_id = (note or {}).get("node_id", "") if isinstance(note, dict) else ""
        if node_id:
            trace.fact_ids.append(node_id)
        return self._format_evidence(result)

    def _charge(self, started: float, tokens: int = 0) -> None:
        """每轮调用后计入统一预算；触顶抛 BudgetExhausted。"""
        if self.budget is None:
            return
        self.budget.charge(
            rounds=1,
            tokens=tokens,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _known_ids(self):
        """图客户端能给出真实节点 id 时返回集合，否则返回 None（宽松模式）。"""
        getter = getattr(self.graph, "known_ids", None)
        if not callable(getter):
            return None
        try:
            return set(getter())
        except Exception:  # noqa: BLE001 - 拿不到就退回宽松模式
            return None

    def _role_note_with_provenance(self, role, kind, content, *, reason, evidence,
                                   source="", confidence=0.0, meta=None):
        """图客户端支持 provenance 字段就带上；老实现（只认 reason/evidence）照旧可用。"""
        fn = self.graph.role_note
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        extra = {}
        if "source" in params:
            extra["source"] = source
        if "confidence" in params:
            extra["confidence"] = float(confidence or 0.0)
        if meta and "meta" in params:
            extra["meta"] = meta
        return fn(role, kind, content, reason=reason, evidence=evidence, **extra)

    @staticmethod
    def _format_evidence(result: ResearchResult) -> str:
        lines = ["[RESEARCH EVIDENCE]", result.answer.strip()]
        for index, item in enumerate(result.evidence, start=1):
            quote = (item.quote or "").strip()
            lines.append(f"({index}) {item.source} — {quote}" if quote else f"({index}) {item.source}")
        lines.append(f"[confidence={result.confidence:.2f} rounds={result.retrieval_rounds}]")
        return "\n".join(lines)

    # ---------- 内部 ----------

    def _call(self, prompt: str, role: str, llm: Callable[[str], str] = None) -> str:
        """按角色的重试/超时预算调一次模型，并把这次调用记进 trace（成功也记）。"""
        fn = llm or self.llm
        policy = self._policy_for(role)
        started = time.perf_counter()
        result = call_with_policy(lambda: fn(prompt) or "", policy)
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._trace.retries += max(0, result.attempts - 1)
        self._emit_trace(
            role=role,
            prompt=prompt,
            result=result,
            latency_ms=latency_ms,
            outcome="ok" if result.ok else "error",
            error=result.error,
            stop_reason="model_call",
        )
        if not result.ok:
            raise OrchestrationError(
                f"{role} 调用失败（{result.attempts} 次）：{result.error or '未知错误'}"
            )
        return result.value

    def _call_json(self, prompt: str, role: str, llm: Callable[[str], str] = None) -> dict:
        payload = parse_json_object(self._call(prompt, role, llm))
        if payload is None:
            raise OrchestrationError(f"{role} 输出无法解析为 JSON")
        return payload
