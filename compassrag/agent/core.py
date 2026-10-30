"""agent 主循环（第 2 代：scripted 与 decision 收敛到同一状态机）。

控制流：`plan → (retrieve → verify)* → answer → finish`

- **scripted**（`use_decision=False`）：plan 固定为"原问题（+改写变体）单轮"，verify 关闭；
- **decision**（`use_decision=True`）：plan 走 LLM 路由与子问题，verify 走充分度判定与早停。

两种形态共用同一套证据累积/截断、工具调用记录、答案生成与预算阶梯、轨迹落盘；
开关只决定"计划节点与验证节点是否启用"，不再维护两套逻辑。

行为等价守卫：scripted 路径的提示词、预算阶梯、证据顺序与截断必须与第 1 代逐位一致
（naive 行 EM 0.300 / F1 0.422 / 3927 token/题）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .schemas import (
    AnswerTrace,
    EvidenceCheck,
    QuerySource,
    RepairAction,
    Route,
    RouteDecision,
    SearchRequest,
    StopReason,
)
from .state import AgentState, Budget
from .tools import ToolRegistry, build_registry

from ..decision.planner import make_plan
from ..diagnose.repair import classify_failures, choose_repair_query, plan_repair
from ..diagnose.verifier import verify_answer
from ..decision.sufficiency import check_sufficiency
from ..query.rewrite import build_query_variants_sourced
from ..retrieval.fusion import near_dup_merge, rrf_fuse

ANSWER_PROMPT = """Answer the question based only on the evidence passages below.
Give a short, direct answer (a few words). If the evidence is insufficient, answer "unknown".

Evidence:
{context}

Question: {question}
Answer:"""

WORD_LIMIT_TMPL = ("\nAnswer with at most {n} words. No titles, no explanations, "
                  "no parentheticals — just the answer itself.")

DIRECT_ANSWER_PROMPT = """Answer the question directly from your own knowledge.
No retrieval was needed for this question. If you are not sure, answer "unknown".

Question: {question}
Answer:"""

RETRY_NUDGE = ("Conclude now without further analysis. Do not re-read or re-check the passages. "
               "Reply with at most 5 words: the answer if the evidence contains it, otherwise \"unknown\".")


@dataclass
class AgentConfig:
    top_k: int = 10
    max_rounds: int = 1          # scripted：固定一轮；use_decision：检索预算上限
    max_evidence: int = 20       # 送入答案上下文的最大证据块数（多轮累积后截断）
    use_dense: bool = True       # 混合检索：BM25 + 稠密向量两路 RRF（naive 级已含）
    use_rewrite: bool = False    # S4：多跳分解 + HyDE
    use_wiki: bool = False       # S3：wiki 索引视图（已实测无净增益，保留开关作负结果复现）
    use_kg: bool = False         # S3 重构：实体关系图 + PPR 三路等权融合（+③ 级）
    use_decision: bool = False   # S5：LLM 接管控制流（路由 / 计划 / 早停）
    answer_max_tokens: int = 1024                # 含思维链；过小会让推理吃光预算、答案为空
    answer_retry_budgets: tuple = (2048, 4096)   # 空答案重试预算阶梯（实测逐级提高可救回打转）
    # ---- 工具与预算（M1/M2）----
    tool_timeout_s: float = 0.0      # 0 = 不强制超时（进程内检索无可靠中断点）
    max_total_tokens: int = 0        # 0 = 不限
    max_latency_ms: float = 0.0
    max_cost_usd: float = 0.0
    price_per_1k_prompt: float = 0.0
    price_per_1k_completion: float = 0.0
    capture_trace: bool = True       # 是否在结果里带回 AnswerTrace
    use_graph: bool = True           # 阶段 C：默认走 LangGraph runner（False = 旧顺序实现，对照用）
    strict_answer_words: int = 0     # 0=不改提示词（保持历史口径）；>0=加硬性词数上限，需实验验证
    # ---- 证据闭环（M2）----
    use_verifier: bool = False       # claim 级核验（独立一次调用，不改编答案协议）
    max_repairs: int = 1             # 全局修复次数上限；用尽后拒答
    refuse_on_empty_evidence: bool = False  # 无证据时不调模型，直接 unknown

    def budget(self) -> Budget:
        return Budget(
            max_rounds=max(1, int(self.max_rounds)),
            max_total_tokens=int(self.max_total_tokens),
            max_latency_ms=float(self.max_latency_ms),
            max_cost_usd=float(self.max_cost_usd),
            price_per_1k_prompt=float(self.price_per_1k_prompt),
            price_per_1k_completion=float(self.price_per_1k_completion),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "top_k": self.top_k, "max_rounds": self.max_rounds,
            "max_evidence": self.max_evidence, "use_dense": self.use_dense,
            "use_rewrite": self.use_rewrite, "use_wiki": self.use_wiki,
            "use_kg": self.use_kg, "use_decision": self.use_decision,
            "answer_max_tokens": self.answer_max_tokens,
            "answer_retry_budgets": list(self.answer_retry_budgets),
            "tool_timeout_s": self.tool_timeout_s,
            "max_total_tokens": self.max_total_tokens,
            "max_latency_ms": self.max_latency_ms,
            "max_cost_usd": self.max_cost_usd,
            "use_graph": self.use_graph,
            "strict_answer_words": self.strict_answer_words,
            "use_verifier": self.use_verifier,
            "max_repairs": self.max_repairs,
            "refuse_on_empty_evidence": self.refuse_on_empty_evidence,
        }


@dataclass
class AgentResult:
    answer: str
    evidence: list[str]          # 送入上下文的 chunk_id（按融合排名）
    rounds: int                  # 实际检索轮数（direct 路由为 0）
    prompt_tokens: int           # 全部 LLM 调用聚合（含 plan / sufficiency / answer）
    completion_tokens: int
    finish_reasons: list[str] = field(default_factory=list)   # 答案调用各次
    n_calls: int = 1             # 答案调用次数（含重试）
    queries: list[str] = field(default_factory=list)          # 实际用于检索的查询
    n_llm_calls: int = 1         # 全部 LLM 调用次数（成本诊断）
    route: str = ""              # 决策形态下的路由（direct/single/multi）
    stop_reason: str = ""        # 为什么停在这里（M1 起落盘）
    trace: AnswerTrace | None = None


class Agent:
    """tools: {name: obj.search(query, k)}；chunk_store: {chunk_id: chunk}。

    工具对象只需有 `search(query, k=...)`；有 `vector(cid)` 时一并用于近重合并。
    """

    def __init__(self, llm, tools: dict, chunk_store: dict, config: AgentConfig | None = None):
        self.llm = llm
        self.tools = tools
        self.store = chunk_store
        self.config = config or AgentConfig()
        self.registry: ToolRegistry = build_registry(tools, timeout_s=self.config.tool_timeout_s)

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    def answer(self, question: str, *, run_id: str = "", on_event=None,
               cancel_check=None) -> AgentResult:
        if self.config.use_graph:
            # 延迟导入：graph 依赖 core，模块级导入会成环
            from .graph import GraphRunner
            return GraphRunner(self).run(question, run_id=run_id, on_event=on_event,
                                         cancel_check=cancel_check)
        return self._answer_sequential(question, run_id=run_id, on_event=on_event,
                                       cancel_check=cancel_check)

    def _answer_sequential(self, question: str, *, run_id: str = "", on_event=None,
                           cancel_check=None) -> AgentResult:
        state = self._new_state(question, run_id=run_id, on_event=on_event,
                                cancel_check=cancel_check)
        route, stop = self._control_flow(state)
        state.route = route
        if state.check_cancel():
            return self._finish_cancelled(state, route)
        stop = self._finalize_stop(state, stop)
        state.truncate_evidence(self.config.max_evidence)

        if self.config.refuse_on_empty_evidence and not state.evidence:
            # 没有证据就不该生成事实：这一题一次模型调用都不发
            state.step("answer", detail={"refused": True, "reason": "no_evidence"})
            state.trace.n_answer_calls = 0
            calls: list = []
            text, stop = "unknown", StopReason.EMPTY_EVIDENCE
        elif (budget_hit := state.budget_stop()) is not None:
            state.step("answer", detail={"refused": True, "reason": budget_hit.value})
            state.trace.n_answer_calls = 0
            calls = []
            text, stop = "unknown", budget_hit
        else:
            text, calls, stop = self._answer_and_verify(state, route, stop)

        state.finish(answer=text, stop_reason=stop, route=route)
        state.trace.evidence_titles = [
            self.store[cid]["title"] for cid in state.evidence if cid in self.store]
        return AgentResult(
            answer=text,
            evidence=list(state.evidence),
            rounds=state.rounds,
            prompt_tokens=state.usage.prompt_tokens,
            completion_tokens=state.usage.completion_tokens,
            finish_reasons=[c.finish_reason for c in calls],
            n_calls=state.trace.n_answer_calls,
            queries=[q.query for q in state.trace.queries],
            n_llm_calls=state.trace.n_llm_calls,
            route=route.value,
            stop_reason=stop.value,
            trace=state.trace if self.config.capture_trace else None,
        )

    def _new_state(self, question: str, *, run_id: str = "", on_event=None,
                   cancel_check=None) -> AgentState:
        """建状态（含事件出口与取消钩子）。"""
        return AgentState(question, budget=self.config.budget(), config=self.config.snapshot(),
                          run_id=run_id, on_event=on_event, cancel_check=cancel_check)

    def _finish_cancelled(self, state: AgentState, route: "Route") -> AgentResult:
        """取消后不再发起任何模型/检索调用，直接以 cancelled 收尾。"""
        state.step("finish", tag="cancelled", detail={"reason": "client_cancelled"})
        state.finish(answer="", stop_reason=StopReason.CANCELLED, route=route)
        state.trace.evidence_titles = [self.store[cid]["title"]
                                       for cid in state.evidence if cid in self.store]
        return AgentResult(
            answer="", evidence=list(state.evidence), rounds=state.rounds,
            prompt_tokens=state.usage.prompt_tokens,
            completion_tokens=state.usage.completion_tokens,
            finish_reasons=[], n_calls=state.trace.n_answer_calls,
            queries=[q.query for q in state.trace.queries],
            n_llm_calls=state.trace.n_llm_calls,
            route=route.value, stop_reason=StopReason.CANCELLED.value,
            trace=state.trace if self.config.capture_trace else None,
        )

    def _control_flow(self, state: AgentState) -> tuple[Route, StopReason]:
        """plan / retrieve / verify 的控制流；scripted 与 decision 只差开关。"""
        if state.check_cancel():
            return Route.SINGLE, StopReason.CANCELLED
        if self.config.use_decision:
            decision = self._plan(state)
            if decision.route is Route.DIRECT:
                return decision.route, StopReason.DIRECT
            if state.check_cancel():
                return decision.route, StopReason.CANCELLED
            return decision.route, self._decide_loop(state, decision)
        return Route.SINGLE, self._scripted_round(state)

    # ------------------------------------------------------------------ #
    # 证据闭环：核验 → 分类 → 有限修复 → 拒答
    # ------------------------------------------------------------------ #

    def _answer_and_verify(self, state: AgentState, route: Route,
                           stop: StopReason) -> tuple[str, list, StopReason]:
        calls = self._answer_with_evidence(state)
        for resp in calls:
            state.add_llm_call(resp)
        state.trace.n_answer_calls = len(calls)
        text = calls[-1].text.strip()
        if state.check_cancel():
            return "", calls, StopReason.CANCELLED
        if not self.config.use_verifier:
            return text, calls, stop

        if state.check_cancel():
            return "", calls, StopReason.CANCELLED

        verification = self._verify(state, text)
        if state.check_cancel():
            return "", calls, StopReason.CANCELLED
        failures = self._classify(state, verification, answer_empty=not text)
        state.trace.verification = verification
        state.trace.failures = list(failures)
        if not failures or not verification.needs_repair:
            return text, calls, stop

        action = plan_repair(failures, repairs_used=state.repairs_used,
                             max_repairs=self.config.max_repairs)
        if action is None:
            return text, calls, stop
        budget_hit = state.budget_stop()
        if action is RepairAction.REFUSE or budget_hit is not None:
            state.trace.repairs.append(RepairAction.REFUSE)
            return "unknown", calls, budget_hit or StopReason.INSUFFICIENT_EVIDENCE

        # 执行一次补检索 + 重答：查询由确定性规则选出，不额外调模型
        query = choose_repair_query(action, claim_text=_first_unsupported(verification),
                                    next_query=state.next_query, question=state.question)
        if not query:
            state.trace.repairs.append(RepairAction.REFUSE)
            return "unknown", calls, StopReason.INSUFFICIENT_EVIDENCE
        state.repairs_used += 1
        state.trace.repairs.append(action)
        if state.check_cancel():
            return "", calls, StopReason.CANCELLED
        state.record_query(query, QuerySource.REPAIR)
        views = self.repair_views(action)
        state.mark_new_evidence(query, self._retrieve_and_collect(
            state, query, QuerySource.REPAIR, only=views))
        state.truncate_evidence(self.config.max_evidence)

        retry_calls = self._answer_with_evidence(state)
        for resp in retry_calls:
            state.add_llm_call(resp)
        state.trace.n_answer_calls += len(retry_calls)
        text2 = retry_calls[-1].text.strip()
        if state.check_cancel():
            return "", calls + retry_calls, StopReason.CANCELLED
        verification2 = self._verify(state, text2)
        if state.check_cancel():
            return "", calls + retry_calls, StopReason.CANCELLED
        failures2 = self._classify(state, verification2, answer_empty=not text2)
        state.trace.verification = verification2
        state.trace.failures = list(failures2)
        all_calls = calls + retry_calls
        if verification2.needs_repair:
            # 修一次仍不成立 ⇒ 不再编，诚实拒答
            state.trace.repairs.append(RepairAction.REFUSE)
            return "unknown", all_calls, StopReason.INSUFFICIENT_EVIDENCE
        return text2, all_calls, stop

    def _verify(self, state: AgentState, answer: str):
        evidence = [(cid, f"{self.store[cid]['title']}: {self.store[cid]['text']}")
                    for cid in state.evidence if cid in self.store]
        result, calls = verify_answer(self.llm, state.question, answer, evidence,
                                      cancel_check=state.check_cancel)
        for resp in calls:
            state.add_llm_call(resp)
        state.step("verify", tag="verify", detail={
            "claims": len(result.claims), "unsupported": result.unsupported,
            "source_conflict": result.source_conflict, "parsed": result.parsed,
        })
        if not result.parsed:
            state.trace.parse_fallbacks += 1
        return result

    @staticmethod
    def _classify(state: AgentState, verification, *, answer_empty: bool) -> list:
        from ..agent.schemas import FailureClass  # 局部导入，避免模块级循环
        return classify_failures(
            tool_outcomes=[c.outcome for c in state.trace.tool_calls],
            query_new_evidence=[q.new_evidence for q in state.trace.queries],
            verification=verification,
            answer_empty=answer_empty,
            sufficiency_stuck=state.sufficiency_stuck,
        )

    @staticmethod
    def _finalize_stop(state: AgentState, stop: StopReason) -> StopReason:
        """没有证据时必须说清是"检索真的没有"还是"工具坏了"。"""
        if stop is not StopReason.ANSWERED or state.evidence:
            return stop
        calls = state.trace.tool_calls
        broken = {"error", "timeout", "invalid_params", "index_missing"}
        if calls and all(c.outcome in broken for c in calls):
            return StopReason.TOOL_FAILURE
        return StopReason.EMPTY_EVIDENCE

    # ------------------------------------------------------------------ #
    # plan 节点
    # ------------------------------------------------------------------ #

    def _plan(self, state: AgentState) -> RouteDecision:
        """调 planner，并把它的输出**立刻收进 schema**——控制流只认 schema。"""
        if state.check_cancel():
            return RouteDecision(route=Route.SINGLE)
        plan = make_plan(self.llm, state.question, cancel_check=state.check_cancel)
        for resp in plan.calls:
            state.add_llm_call(resp)
        if state.check_cancel():
            state.step("plan", tag="cancelled", detail={"reason": "client_cancelled"})
            return RouteDecision(route=Route.SINGLE)
        decision = RouteDecision(route=Route(plan.route),
                                 subquestions=list(plan.subquestions),
                                 parsed=plan.parsed,
                                 fallback_reason=plan.fallback_reason)
        state.step("plan", tag="plan", detail={
            "route": decision.route.value, "subquestions": list(decision.subquestions),
            "parsed": decision.parsed, "fallback_reason": decision.fallback_reason,
        })
        if not decision.parsed:
            state.trace.parse_fallbacks += 1
        return decision

    # ------------------------------------------------------------------ #
    # scripted：固定单轮（可带改写变体）
    # ------------------------------------------------------------------ #

    def _scripted_round(self, state: AgentState) -> StopReason:
        if state.check_cancel():
            return StopReason.CANCELLED
        variants = build_query_variants_sourced(self.llm, state.question,
                                                cancel_check=state.check_cancel) \
            if self.config.use_rewrite else [(state.question, QuerySource.ORIGINAL)]
        if state.check_cancel():
            return StopReason.CANCELLED
        for query, source in variants:
            state.record_query(query, QuerySource(source))
        for query, source in variants:
            if state.check_cancel():
                return StopReason.CANCELLED
            new = self._retrieve_and_collect(state, query, source)
            state.mark_new_evidence(query, new)
        state.rounds = 1
        return StopReason.ANSWERED

    # ------------------------------------------------------------------ #
    # decision：路由 + 多跳 + 早停
    # ------------------------------------------------------------------ #

    def _decide_loop(self, state: AgentState, decision: RouteDecision) -> StopReason:
        if decision.route is Route.MULTI and decision.subquestions:
            # Keep the original question as the first retrieval anchor. The KG query
            # entity cache is keyed by the original wording; subquestions otherwise
            # fall back to weaker embedding-only seed selection and lose coverage.
            pending = [(state.question, QuerySource.ORIGINAL)]
            pending.extend((q, QuerySource.SUBQUESTION)
                           for q in decision.subquestions if q != state.question)
        else:
            pending = [(state.question, QuerySource.ORIGINAL)]

        stop = StopReason.ANSWERED
        for _ in range(max(1, self.config.max_rounds)):
            if not pending:
                break
            if state.check_cancel():
                return StopReason.CANCELLED
            if (hit := state.budget_stop()) is not None:
                stop = hit
                break
            query, source = pending.pop(0)
            state.record_query(query, source)
            new = self._retrieve_and_collect(state, query, source)
            state.mark_new_evidence(query, new)
            state.rounds += 1

            check = check_sufficiency(self.llm, state.question, self._format(state.evidence),
                                      cancel_check=state.check_cancel)
            for resp in check.calls:
                state.add_llm_call(resp)
            if state.check_cancel():
                return StopReason.CANCELLED
            evidence_check = EvidenceCheck(sufficient=check.ok, next_query=check.next_query,
                                           parsed=check.parsed)
            state.step("verify", tag="sufficiency", detail={
                "sufficient": evidence_check.sufficient,
                "next_query": evidence_check.next_query,
                "parsed": evidence_check.parsed, "new_evidence": new,
            })
            if not evidence_check.parsed:
                state.trace.parse_fallbacks += 1
            if evidence_check.sufficient:
                stop = StopReason.EARLY_STOP
                break
            if state.check_cancel():
                return StopReason.CANCELLED
            if (hit := state.budget_stop()) is not None:
                stop = hit
                break
            if evidence_check.next_query:
                state.next_query = evidence_check.next_query
            else:
                state.sufficiency_stuck = True
            if evidence_check.next_query and evidence_check.next_query not in [q for q, _ in pending]:
                pending.insert(0, (evidence_check.next_query, QuerySource.SUFFICIENCY))
            if not pending:
                break
        else:
            stop = StopReason.BUDGET_ROUNDS
        return stop

    # ------------------------------------------------------------------ #
    # retrieve 节点
    # ------------------------------------------------------------------ #

    def _retrieve_and_collect(self, state: AgentState, query: str,
                             source: QuerySource, *,
                             only: list[str] | None = None) -> int:
        """一条查询 × 全部工具 → RRF → 近重合并 → 去重累积；返回新增证据数。"""
        if state.check_cancel():
            return 0
        request = SearchRequest(query=query, top_k=self.config.top_k)
        calls = self.registry.run_all(request, names=only, cancel_check=state.check_cancel)
        for call in calls:
            state.add_tool_call(call)
        state.step("retrieve", detail={
            "query": query, "source": QuerySource(source).value,
            "tools": [{"tool": c.tool_name, "outcome": c.outcome, "hits": len(c.hits)}
                      for c in calls],
        })
        if state.check_cancel():
            return 0
        routes = [c.chunk_ids for c in calls if c.ok]
        # 近重合并只在 use_rewrite 且 dense 工具**真的带向量**时启用（与第 1 代语义一致）
        dense_spec = self.registry.get("dense")
        has_vectors = dense_spec is not None and callable(
            getattr(getattr(dense_spec, "tool", None), "vector", None))
        vec_fn = dense_spec.vector if (self.config.use_rewrite and has_vectors) else None
        top_k = int(self.config.top_k)
        wide = top_k * 2 if vec_fn is not None else top_k
        fused = rrf_fuse(routes, top_n=wide)
        if vec_fn is not None:
            fused = near_dup_merge(fused, vec_fn)[:top_k]
        ids = [cid for cid, _ in fused][:top_k]
        return state.add_evidence(ids)

    # ------------------------------------------------------------------ #
    # answer 节点
    # ------------------------------------------------------------------ #

    def _answer_with_evidence(self, state: AgentState) -> list:
        """答案生成 + 空答案预算阶梯重试；返回各次 LLMResponse（末次为准）。"""
        messages = [
            {"role": "system",
             "content": "You answer multi-hop questions using only the provided evidence."},
            {"role": "user",
             "content": self._answer_prompt(state)},
        ]
        resp = self.llm.chat(messages, tag="answer", max_tokens=self.config.answer_max_tokens)
        calls = [resp]
        for budget in self.config.answer_retry_budgets:
            if state.check_cancel():
                break
            if resp.text.strip():
                break
            resp = self.llm.chat(messages + [{"role": "user", "content": RETRY_NUDGE}],
                                 tag="answer_retry", max_tokens=int(budget))
            calls.append(resp)
        state.step("answer", tag="answer", detail={
            "n_calls": len(calls),
            "finish_reasons": [c.finish_reason for c in calls],
            "empty": not calls[-1].text.strip(),
        })
        return calls

    def repair_views(self, action: RepairAction) -> list[str] | None:
        """`switch_view` 时只走一路信号（实体图优先，其次稠密）；其它动作返回 None = 全量。

        这是"切换检索视图"的真实实现：不是把三路再打一遍，而是换一个证据来源看。
        """
        if action is not RepairAction.SWITCH_VIEW:
            return None
        names = self.registry.names()
        if len(names) <= 1:
            return None
        for preferred in ("kg", "dense", "wiki"):
            if preferred in names:
                return [preferred]
        return None

    def _answer_prompt(self, state: AgentState) -> str:
        """答案提示词。

        * `direct` 路由（判定"不需要检索"）用常识提示词——否则"只准用证据"会把它逼成 unknown；
        * 其余路径用证据提示词；`strict_answer_words=0` 时与历史版本**逐字节一致**（锚点要求）。
        """
        if state.route is Route.DIRECT:
            return DIRECT_ANSWER_PROMPT.format(question=state.question)
        prompt = ANSWER_PROMPT.format(context=self._format(state.evidence),
                                      question=state.question)
        if self.config.strict_answer_words > 0:
            prompt += WORD_LIMIT_TMPL.format(n=int(self.config.strict_answer_words))
        return prompt

    def _format(self, evidence_ids: list[str]) -> str:
        lines = []
        for cid in evidence_ids:
            c = self.store.get(cid)
            if c is not None:
                lines.append(f"[{len(lines) + 1}] {c['title']}: {c['text']}")
        return "\n".join(lines) if lines else "(no evidence retrieved)"


def _first_unsupported(verification) -> str:
    """取第一条未被支撑的断言原文——它点名了缺的证据，适合直接当补检索查询。"""
    for claim in getattr(verification, "claims", []) or []:
        if not claim.supported:
            return claim.text
    return ""
