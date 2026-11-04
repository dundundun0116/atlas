"""任务 API 路由（Task 1）：薄壳——只做协议转换，业务全在 `atlas/runtime.py`。

好处：调用方与测试可以完全绕开 HTTP（`AgentRuntime` 直接可用），而 HTTP 层只负责
把 `RuntimeFailure` 的 `kind/status_code` 映射成统一错误体。
"""

import time
from dataclasses import replace
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from atlas.api import schemas
from atlas.api.graph import RepositoryGraph
from atlas.orchestration import Orchestrator
from atlas.runtime import (
    AgentRuntime,
    ModelTimeout,
    RuntimeFailure,
    StateConflict,
    Unrecoverable,
)
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository
from atlas.tools import ResearchRequest, ResearchToolError


def create_app(repository: GraphRepository,
               tracer_factory: Optional[Callable[[str], Any]] = None,
               research_tool: Any = None,
               orchestrate_llm: Optional[Callable[[str], str]] = None,
               orchestrate_budget: Any = None,
               context_budget_tokens: Optional[int] = None,
               context_token_counter: Optional[Callable[[str], int]] = None) -> FastAPI:
    app = FastAPI(title="Atlas Task Runtime API", version="0.3.0")
    runtime = AgentRuntime(repository, tracer_factory=tracer_factory)
    terminal_states = ("completed", "failed")

    @app.exception_handler(RuntimeFailure)
    async def _runtime_failure(_request, exc: RuntimeFailure):
        body = schemas.ErrorResponse(
            error=schemas.ErrorBody(
                type=exc.kind, message=exc.message, request_id=exc.request_id
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.get("/healthz", response_model=schemas.HealthView)
    def healthz():
        try:
            sessions = repository.session_count()
            storage = "ok"
        except Exception:  # pragma: no cover - 只在存储真的坏时命中
            sessions, storage = -1, "failed"
        return schemas.HealthView(ok=storage == "ok", storage=storage, sessions=sessions)

    # ---------- 任务生命周期 ----------

    @app.post("/tasks", response_model=schemas.AdvanceView)
    def create_task(payload: schemas.CreateTaskRequest):
        return schemas.AdvanceView(**runtime.create_task(
            payload.session_id, payload.goal,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/advance", response_model=schemas.AdvanceView)
    def advance_task(session_id: str, payload: schemas.AdvanceRequest):
        return schemas.AdvanceView(**runtime.advance_task(
            session_id, payload.trigger, reason=payload.reason,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/pause", response_model=schemas.AdvanceView)
    def pause_task(session_id: str, payload: schemas.PauseRequest):
        return schemas.AdvanceView(**runtime.pause_task(
            session_id, reason=payload.reason,
            request_id=payload.request_id, idempotency_key=payload.idempotency_key,
        ))

    @app.post("/tasks/{session_id}/resume", response_model=schemas.AdvanceView)
    def resume_task(session_id: str, payload: schemas.RequestMeta):
        return schemas.AdvanceView(**runtime.resume_task(
            session_id, request_id=payload.request_id,
            idempotency_key=payload.idempotency_key,
        ))

    # ---------- 研究（可选：部署时注入 ResearchTool） ----------

    @app.post("/tasks/{session_id}/research", response_model=schemas.ResearchCallView)
    def research_task(session_id: str, payload: schemas.ResearchCallRequest):
        if research_tool is None:
            raise Unrecoverable(
                "本部署未配置 ResearchTool（启动参数 --compassrag-url）", request_id=payload.request_id
            )
        runtime.get_state(session_id)  # 任务必须存在（不存在 → 404）
        request = ResearchRequest(
            session_id=session_id,
            subtask_id=payload.subtask_id,
            query=payload.query,
            budget_tokens=payload.budget_tokens,
            max_rounds=payload.max_rounds,
        )
        started = time.perf_counter()
        try:
            result = research_tool.research(request)
        except ResearchToolError as exc:
            repository.record_trace(session_id, {
                "trace_id": exc.trace_id or request.trace_id,
                "agent_role": "research", "tool_name": "compassrag",
                "outcome": exc.kind, "stop_reason": exc.kind,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            if exc.kind == "timeout":
                # 上游超时走统一错误体（504 model_timeout），而不是 200 的成功信封
                raise ModelTimeout(exc.message or "研究工具超时", request_id=payload.request_id)
            return schemas.ResearchCallView(
                session_id=session_id, failure_kind=exc.kind,
                failure_message=exc.message, trace_id=exc.trace_id or request.trace_id,
            )
        service = ServiceRuntime(repository, session_id)
        row = service.note(
            "research", result.answer, kind="fact",
            reason=(f"检索 {result.retrieval_rounds} 轮，confidence={result.confidence:.2f}，"
                    f"stop={result.stop_reason or 'unknown'}"),
            evidence=result.evidence_json(),
            source=",".join(item.source for item in result.evidence[:3]),
            confidence=result.confidence,
            idempotency_key=request.idempotency_key,
            meta={
                "research_trace_id": result.trace_id,
                "research_stop_reason": result.stop_reason,
                "research_rounds": result.retrieval_rounds,
                "research_confidence": result.confidence,
            },
        )
        repository.record_trace(session_id, {
            "trace_id": result.trace_id or request.trace_id,
            "agent_role": "research", "tool_name": "compassrag",
            "outcome": "ok", "stop_reason": result.stop_reason,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        return schemas.ResearchCallView(
            session_id=session_id, fact_id=row.node_id, answer=result.answer,
            evidence=[item.to_dict() for item in result.evidence],
            confidence=result.confidence, stop_reason=result.stop_reason,
            trace_id=result.trace_id or request.trace_id,
        )

    # ---------- 完整编排（可选：部署时注入模型） ----------

    @app.post("/tasks/{session_id}/orchestrate", response_model=schemas.OrchestrateView)
    def orchestrate_task(session_id: str, payload: schemas.OrchestrateRequest):
        if orchestrate_llm is None:
            raise Unrecoverable(
                "本部署未配置编排模型（启动参数或 LLM_API_KEY/LLM_MODEL）",
                request_id=payload.request_id,
            )
        view = runtime.get_state(session_id)  # 不存在 → 404
        current = view["state"]
        if current in terminal_states:
            raise StateConflict(f"任务已到终态 {current}，不能再编排", request_id=payload.request_id)
        if current != "planning":
            raise StateConflict(
                f"编排只能从 planning 开始（当前 {current}）", request_id=payload.request_id
            )
        problem = (payload.problem or view.get("goal") or "").strip()
        if not problem:
            raise Unrecoverable("任务没有可用的问题文本", request_id=payload.request_id)

        # 0) 时间规则过期：真实调用点。活跃状态先按 valid_until 作废，再构造上下文，
        #    否则模型会看到本该失效的约束（设计文档 §4.10）。
        runtime.expire_due_nodes(session_id)
        graph = RepositoryGraph(
            repository, session_id,
            **({"context_budget_tokens": context_budget_tokens}
               if context_budget_tokens else {}),
            **({"context_token_counter": context_token_counter}
               if context_token_counter else {}),
        )

        def advance(trigger: str, reason: str = "") -> None:
            """把编排中的状态转移**当场落库**（不是最后再补一遍）。"""
            if runtime.get_state(session_id)["state"] in terminal_states:
                return
            runtime.advance_task(
                session_id, trigger, reason=(reason or "")[:200],
                request_id=payload.request_id,
            )

        budget = orchestrate_budget
        if budget is not None:
            # 传进来的只是"模板"：每次编排用一份新的预算实例，上一次的消耗不污染这一次。
            budget = budget() if callable(budget) else replace(budget)
        orchestrator = Orchestrator(
            orchestrate_llm, graph,
            research_tool=research_tool,
            budget=budget,
            transition=advance,
            trace_sink=lambda event: repository.record_trace(session_id, event),
            run_id=session_id,
        )
        try:
            answer, trace = orchestrator.solve(
                problem, session_id=session_id, subtask_id=payload.subtask_id,
                research_query=payload.research_query or problem,
            )
        except Exception as exc:
            reason = f"编排失败：{type(exc).__name__}: {exc}"
            repository.record_trace(session_id, {
                "trace_id": f"orch-{payload.request_id or time.time_ns()}",
                "run_id": session_id,
                "agent_role": "orchestrator", "tool_name": "",
                "outcome": "error", "stop_reason": "orchestration_error",
                "error": reason[:300],
            })
            advance("fail", reason)
            raise Unrecoverable(reason, request_id=payload.request_id) from exc

        # 成功也必须**已经**落到终态；没收敛就是接线错误，不能静默当成功。
        final_state = runtime.get_state(session_id)["state"]
        if final_state not in terminal_states:
            reason = f"编排返回但任务未收敛（state={final_state}）"
            advance("fail", reason)
            repository.record_trace(session_id, {
                "trace_id": f"orch-{payload.request_id or time.time_ns()}",
                "run_id": session_id, "agent_role": "orchestrator", "tool_name": "",
                "outcome": "error", "stop_reason": "not_converged",
            })
            raise Unrecoverable(reason, request_id=payload.request_id)

        return schemas.OrchestrateView(
            session_id=session_id, state=final_state, answer=answer,
            route=trace.route, draft=trace.draft, calls=list(trace.calls),
            fact_ids=list(trace.fact_ids),
            research_trace_id=trace.research_trace_id,
            research_error=trace.research_error,
            context_digests=list(trace.context_digests),
            retries=trace.retries,
            workflow_version=str(view.get("workflow_version") or ""),
        )

    # ---------- 读 ----------

    @app.get("/tasks/{session_id}", response_model=schemas.TaskStateView)
    def get_task(session_id: str):
        return schemas.TaskStateView(**runtime.get_state(session_id))

    @app.post("/tasks/{session_id}/restore", response_model=schemas.TaskStateView)
    def restore_task(session_id: str):
        return schemas.TaskStateView(**runtime.restore_task(session_id))

    @app.get("/tasks/{session_id}/trace", response_model=schemas.TraceView)
    def get_trace(session_id: str):
        return schemas.TraceView(**runtime.get_trace(session_id))

    @app.get("/tasks/{session_id}/contexts", response_model=schemas.ContextPackageListView)
    def get_contexts(session_id: str, limit: int = 50):
        """这次任务里每次模型调用实际看到的上下文（digest / 分区 token / 引用）。"""
        runtime.get_state(session_id)  # 不存在 → 404
        packages = repository.context_packages(session_id, limit=limit)
        return schemas.ContextPackageListView(
            session_id=session_id,
            packages=[schemas.ContextPackageView(**item) for item in packages],
        )

    @app.get("/tasks/{session_id}/report", response_model=schemas.ReportView)
    def get_report(session_id: str):
        return schemas.ReportView(**runtime.get_report(session_id))

    @app.get("/tasks/{session_id}/replay")
    def replay_task(session_id: str) -> Dict[str, Any]:
        return runtime.replay(session_id)

    return app
