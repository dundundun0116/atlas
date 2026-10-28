"""LangGraph 控制流（阶段 C）。

图结构（与 `Agent.answer` 等价，见 `tests/test_agent_graph.py` 的对照测试）：

    plan ──direct──────────────┐
      │                       ├─> prep ─> answer ─┬─(claims 不成立)─> repair ─┐
      └─scripted─> retrieve_all ─┘                └────────────────> finish   │
              └─decision─> retrieve_one ─> verify ──(循环/收敛)───────────────┘
                                                     answer <── repair

边界（计划里写明的那条）：**LangGraph 只负责跳转**。证据、工具调用、用量、停止原因和
claim 核验结果全部记在 `AgentState` / `AnswerTrace`（项目自己的 schema），框架状态里
只放"下一步走哪条边"的最小信息——所以换掉框架不会丢业务事实。

这个 runner 同时服务单题调用与批量评测；`Agent.answer` 保持原样作为对照实现，
二者由对照测试锁死。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .core import Agent, AgentResult
from .schemas import (
    EvidenceCheck,
    QuerySource,
    RepairAction,
    Route,
    RouteDecision,
    StopReason,
)
from .state import AgentState

from ..decision.sufficiency import check_sufficiency
from ..diagnose.repair import choose_repair_query, plan_repair
from ..query.rewrite import build_query_variants_sourced


class GraphState(TypedDict, total=False):
    st: AgentState                      # 业务状态（唯一真源）
    decision: RouteDecision | None
    pending: list[dict[str, str]]       # [{"query": ..., "source": ...}]
    scripted: bool
    stop: str
    answer: str
    calls: list[Any]
    repair_query: str
    repair_action: str
    repaired: bool
    refused: bool


class GraphRunner:
    """把一个 `Agent` 包成 LangGraph 图；节点复用 agent 的检索/答案/核验组件。"""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.graph = self._build()

    # -- 图 ---------------------------------------------------------------- #

    def _build(self):
        g = StateGraph(GraphState)
        g.add_node("plan", self._plan)
        g.add_node("retrieve_all", self._retrieve_all)
        g.add_node("retrieve_one", self._retrieve_one)
        g.add_node("verify", self._verify)
        g.add_node("prep", self._prep)
        g.add_node("answer", self._answer)
        g.add_node("repair", self._repair)
        g.add_node("finish", self._finish)

        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", self._after_plan,
                                {"prep": "prep", "retrieve_all": "retrieve_all",
                                 "retrieve_one": "retrieve_one"})
        g.add_edge("retrieve_all", "prep")
        g.add_edge("retrieve_one", "verify")
        g.add_conditional_edges("verify", self._after_verify,
                                {"retrieve_one": "retrieve_one", "prep": "prep"})
        g.add_conditional_edges("prep", self._after_prep,
                                {"answer": "answer", "finish": "finish"})
        g.add_conditional_edges("answer", self._after_answer,
                                {"repair": "repair", "finish": "finish"})
        g.add_edge("repair", "answer")
        g.add_edge("finish", END)
        return g.compile()

    # -- 节点 -------------------------------------------------------------- #

    def _plan(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        if st.check_cancel():
            return {"stop": StopReason.CANCELLED.value, "pending": []}
        if agent.config.use_decision:
            decision = agent._plan(st)
            if decision.route is Route.DIRECT:
                return {"decision": decision, "pending": [],
                        "stop": StopReason.DIRECT.value}
            if decision.route is Route.MULTI and decision.subquestions:
                pending = [{"query": q, "source": QuerySource.SUBQUESTION.value}
                           for q in decision.subquestions]
            else:
                pending = [{"query": st.question, "source": QuerySource.ORIGINAL.value}]
            return {"decision": decision, "pending": pending}

        if agent.config.use_rewrite:
            variants = build_query_variants_sourced(agent.llm, st.question)
        else:
            variants = [(st.question, QuerySource.ORIGINAL.value)]
        for query, source in variants:
            st.record_query(query, QuerySource(source))
        return {"scripted": True,
                "pending": [{"query": q, "source": str(s)} for q, s in variants]}

    def _after_plan(self, gs: GraphState) -> str:
        if gs.get("stop") == StopReason.DIRECT.value:
            return "prep"
        if gs.get("scripted"):
            return "retrieve_all"
        if gs["st"].budget_stop() is not None:
            return "prep"
        return "retrieve_one"

    def _retrieve_all(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        for item in gs["pending"]:
            new = agent._retrieve_and_collect(st, item["query"], QuerySource(item["source"]))
            st.mark_new_evidence(item["query"], new)
        st.rounds = 1
        return {"pending": [], "stop": StopReason.ANSWERED.value}

    def _retrieve_one(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        if st.check_cancel():
            return {"stop": StopReason.CANCELLED.value, "pending": []}
        item, rest = gs["pending"][0], gs["pending"][1:]
        st.record_query(item["query"], QuerySource(item["source"]))
        new = agent._retrieve_and_collect(st, item["query"], QuerySource(item["source"]))
        st.mark_new_evidence(item["query"], new)
        st.rounds += 1
        return {"pending": rest}

    def _verify(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        check = check_sufficiency(agent.llm, st.question, agent._format(st.evidence))
        for resp in check.calls:
            st.add_llm_call(resp)
        result = EvidenceCheck(sufficient=check.ok, next_query=check.next_query,
                               parsed=check.parsed)
        st.step("verify", tag="sufficiency", detail={
            "sufficient": result.sufficient, "next_query": result.next_query,
            "parsed": result.parsed,
        })
        if not result.parsed:
            st.trace.parse_fallbacks += 1
        if result.sufficient:
            return {"stop": StopReason.EARLY_STOP.value}
        budget_hit = st.budget_stop()
        if budget_hit is not None:
            return {"stop": budget_hit.value}
        if result.next_query:
            st.next_query = result.next_query
        else:
            st.sufficiency_stuck = True

        # 路由函数不能改状态（LangGraph 只接受节点的返回值），所以插入与收敛判断都放在节点里
        pending = list(gs["pending"])
        if st.next_query and st.next_query not in [p["query"] for p in pending]:
            pending.insert(0, {"query": st.next_query,
                               "source": QuerySource.SUFFICIENCY.value})
        if not pending:
            return {"pending": [], "stop": StopReason.ANSWERED.value}
        if st.rounds >= agent.config.max_rounds:
            return {"pending": pending, "stop": StopReason.BUDGET_ROUNDS.value}
        return {"pending": pending}

    def _after_verify(self, gs: GraphState) -> str:
        return "prep" if gs.get("stop") else "retrieve_one"

    def _prep(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        if gs.get("stop") == StopReason.CANCELLED.value or st.check_cancel():
            st.step("finish", tag="cancelled", detail={"reason": "client_cancelled"})
            return {"stop": StopReason.CANCELLED.value, "answer": "",
                    "calls": [], "refused": True}
        stop = StopReason(gs.get("stop") or StopReason.ANSWERED.value)
        stop = agent._finalize_stop(st, stop)
        st.truncate_evidence(agent.config.max_evidence)
        if agent.config.refuse_on_empty_evidence and not st.evidence:
            st.step("answer", detail={"refused": True, "reason": "no_evidence"})
            st.trace.n_answer_calls = 0
            return {"stop": StopReason.EMPTY_EVIDENCE.value, "answer": "unknown",
                    "calls": [], "refused": True}
        return {"stop": stop.value}

    def _after_prep(self, gs: GraphState) -> str:
        return "finish" if gs.get("refused") else "answer"

    def _answer(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        if st.check_cancel():
            return {"answer": "", "calls": [], "stop": StopReason.CANCELLED.value,
                    "repaired": True, "refused": True}
        first = not gs.get("repaired")
        calls = agent._answer_with_evidence(st)
        for resp in calls:
            st.add_llm_call(resp)
        if first:
            st.trace.n_answer_calls = len(calls)
        else:
            st.trace.n_answer_calls += len(calls)
        text = calls[-1].text.strip()
        merged_calls = list(gs.get("calls") or []) + calls

        if not agent.config.use_verifier:
            return {"answer": text, "calls": merged_calls, "repaired": True}

        verification = agent._verify(st, text)
        failures = agent._classify(st, verification, answer_empty=not text)
        st.trace.verification = verification
        st.trace.failures = list(failures)
        if not failures or not verification.needs_repair:
            return {"answer": text, "calls": merged_calls, "repaired": True}
        if not first:
            st.trace.repairs.append(RepairAction.REFUSE)
            return {"answer": "unknown", "calls": merged_calls,
                    "stop": StopReason.INSUFFICIENT_EVIDENCE.value, "repaired": True}

        action = plan_repair(failures, repairs_used=st.repairs_used,
                             max_repairs=agent.config.max_repairs)
        budget_hit = st.budget_stop()
        if action is None:
            return {"answer": text, "calls": merged_calls, "repaired": True}
        if action is RepairAction.REFUSE or budget_hit is not None:
            st.trace.repairs.append(RepairAction.REFUSE)
            return {"answer": "unknown", "calls": merged_calls,
                    "stop": (budget_hit or StopReason.INSUFFICIENT_EVIDENCE).value,
                    "repaired": True}
        query = choose_repair_query(action, claim_text=_first_unsupported(verification),
                                    next_query=st.next_query, question=st.question)
        if not query:
            st.trace.repairs.append(RepairAction.REFUSE)
            return {"answer": "unknown", "calls": merged_calls,
                    "stop": StopReason.INSUFFICIENT_EVIDENCE.value, "repaired": True}
        return {"answer": text, "calls": merged_calls, "repair_query": query,
                "repair_action": action.value}

    def _after_answer(self, gs: GraphState) -> str:
        return "repair" if gs.get("repair_query") else "finish"

    def _repair(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        query = gs["repair_query"]
        st.repairs_used += 1
        st.trace.repairs.append(RepairAction(gs["repair_action"]))
        st.record_query(query, QuerySource.REPAIR)
        views = agent.repair_views(RepairAction(gs["repair_action"]))
        st.mark_new_evidence(query, agent._retrieve_and_collect(
            st, query, QuerySource.REPAIR, only=views))
        st.truncate_evidence(agent.config.max_evidence)
        return {"repaired": True, "repair_query": ""}

    def _finish(self, gs: GraphState) -> dict[str, Any]:
        agent, st = self.agent, gs["st"]
        decision = gs.get("decision")
        route = decision.route if decision is not None else Route.SINGLE
        stop = StopReason(gs.get("stop") or StopReason.ANSWERED.value)
        st.step("finish", tag="finish", detail={"stop_reason": stop.value,
                                                "answer_empty": not (gs.get("answer") or "").strip()})
        st.finish(answer=gs.get("answer", ""), stop_reason=stop, route=route)
        st.trace.evidence_titles = [agent.store[cid]["title"]
                                    for cid in st.evidence if cid in agent.store]
        return {}

    # -- 入口 -------------------------------------------------------------- #

    def run(self, question: str, *, run_id: str = "", on_event=None,
            cancel_check=None) -> AgentResult:
        agent = self.agent
        st = AgentState(question, budget=agent.config.budget(),
                        config=agent.config.snapshot(), run_id=run_id,
                        on_event=on_event, cancel_check=cancel_check)
        gs: GraphState = {"st": st}
        final = self.graph.invoke(gs)
        st = final["st"]
        calls = final.get("calls") or []
        route = (final.get("decision").route if final.get("decision") else Route.SINGLE)
        return AgentResult(
            answer=st.trace.answer,
            evidence=list(st.evidence),
            rounds=st.rounds,
            prompt_tokens=st.usage.prompt_tokens,
            completion_tokens=st.usage.completion_tokens,
            finish_reasons=[c.finish_reason for c in calls],
            n_calls=st.trace.n_answer_calls,
            queries=[q.query for q in st.trace.queries],
            n_llm_calls=st.trace.n_llm_calls,
            route=route.value,
            stop_reason=st.trace.stop_reason.value,
            trace=st.trace if agent.config.capture_trace else None,
        )


def run_graph(agent: Agent, question: str) -> AgentResult:
    return GraphRunner(agent).run(question)


def _first_unsupported(verification) -> str:
    for claim in getattr(verification, "claims", []) or []:
        if not claim.supported:
            return claim.text
    return ""


__all__ = ["GraphRunner", "GraphState", "run_graph"]
