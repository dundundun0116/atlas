"""任务 API 路由（Task 1）：薄壳——只做协议转换，业务全在 `atlas/runtime.py`。

好处：调用方与测试可以完全绕开 HTTP（`AgentRuntime` 直接可用），而 HTTP 层只负责
把 `RuntimeFailure` 的 `kind/status_code` 映射成统一错误体。
"""

import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from atlas.api import schemas
from atlas.api.graph import RepositoryGraph
from atlas.orchestration import Orchestrator
from atlas.planning.executors import ResearchStepExecutor
from atlas.planning.recovery import assess_recovery
from atlas.planning.research_plan import RESEARCH_PLAN_WORKFLOW, allowed_tools, build_research_plan
from atlas.planning.runner import PlanRunner
from atlas.planning.schemas import PlanSchemaError, PlanSpec
from atlas.planning.validator import validate_plan
from atlas.runtime import (
    AgentRuntime,
    InvalidRequest,
    ModelTimeout,
    NotFound,
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
               write_tool: Any = None,
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

    # ---------- 计划（DAG 主路径：plan_dag_v1） ----------

    def _plan_view(payload: dict) -> dict:
        return {
            "plan_id": payload["plan_id"],
            "revision": payload["revision"],
            "status": payload["status"],
            "summary": payload["summary"],
            "strategy": payload["strategy"],
            "generated_by": payload["generated_by"],
            "validator_version": payload["validator_version"],
            "created_at": payload["created_at"],
            "activated_at": payload["activated_at"],
            "validation": payload.get("validation_report", {}),
        }

    def _step_views(detail: dict) -> list:
        return [
            {
                "step_id": item["step_id"], "title": item["title"],
                "kind": item["kind"], "status": item["status"],
                "ordinal": item["ordinal"], "tool_name": item["tool_name"],
                "attempt_count": item["attempt_count"],
                "status_reason": item["status_reason"],
                "stale_reason": item["stale_reason"],
            }
            for item in detail["steps"]
        ]

    def _detail_view(session_id: str, detail: dict) -> dict:
        return {
            "session_id": session_id,
            "revisions": repository.plan_revisions(
                session_id, detail["plan"]["plan_id"]),
            "plan": _plan_view(detail["plan"]),
            "steps": _step_views(detail),
            "dependencies": [
                {
                    "from_step_id": item["from_step_id"],
                    "to_step_id": item["to_step_id"],
                    "kind": item["kind"],
                    "binding": dict(item["binding"] or {}),
                }
                for item in detail["dependencies"]
            ],
        }

    @app.post("/tasks/{session_id}/plans", response_model=schemas.PlanDetailView)
    def submit_plan(session_id: str, payload: schemas.PlanSubmitRequest):
        """提交计划候选 → 校验 → 落库；未过校验的绝不激活（§5.4）。"""
        runtime.get_state(session_id)  # 不存在 → 404
        if payload.plan is not None:
            try:
                spec = PlanSpec.from_dict(payload.plan)
            except PlanSchemaError as exc:
                raise InvalidRequest(f"计划结构不合法：{exc}", request_id=payload.request_id)
            tools = None
        else:
            problem = (payload.problem or "").strip()
            if not problem:
                raise InvalidRequest("要么给 plan，要么给 problem", request_id=payload.request_id)
            if payload.with_research and research_tool is None:
                raise Unrecoverable(
                    "本部署未配置 ResearchTool，无法生成含检索步的计划",
                    request_id=payload.request_id,
                )
            spec = build_research_plan(
                problem,
                with_research=payload.with_research,
                with_repair=payload.with_repair,
                budget_tokens=payload.budget_tokens,
            )
            tools = allowed_tools(with_research=payload.with_research)

        report = validate_plan(spec, allowed_tools=tools)
        detail = repository.save_plan(
            session_id, spec, validation_report=report.to_dict(),
            status="validated" if report.ok else "rejected",
            reason=report.error_codes() and "；".join(report.error_codes()) or "",
        )
        plan_id = detail["plan"]["plan_id"]
        if payload.activate and report.ok:
            detail = _activate(session_id, plan_id)
        return _detail_view(session_id, repository.plan_detail(session_id, plan_id))

    @app.get("/tasks/{session_id}/plans", response_model=schemas.PlanListView)
    def list_plans(session_id: str):
        runtime.get_state(session_id)  # 不存在 → 404
        return schemas.PlanListView(
            session_id=session_id,
            plans=[_plan_view(item) for item in repository.plans(session_id)],
        )

    @app.get("/tasks/{session_id}/plans/{plan_id}", response_model=schemas.PlanDetailView)
    def get_plan(session_id: str, plan_id: str):
        runtime.get_state(session_id)
        try:
            detail = repository.plan_detail(session_id, plan_id)
        except KeyError as exc:
            raise NotFound(str(exc), request_id="")
        return _detail_view(session_id, detail)

    def _build_executor(session_id: str, problem: str) -> ResearchStepExecutor:
        """把模型 / 工具 / 上下文 / 预算接成一份 Step 执行器（advance 与 recover 共用）。"""
        graph = RepositoryGraph(
            repository, session_id,
            **({"context_budget_tokens": context_budget_tokens}
               if context_budget_tokens else {}),
            **({"context_token_counter": context_token_counter}
               if context_token_counter else {}),
        )
        return ResearchStepExecutor(
            orchestrate_llm,
            problem=problem,
            research_tool=research_tool,
            context_provider=lambda role, focus: graph.context_package(role, focus=focus).text,
            session_id=session_id,
            budget=replace(orchestrate_budget) if orchestrate_budget is not None else None,
            # 给了 repository 就会把工具调用记成持久化 ToolCall（可对账、不盲重发）
            repository=repository,
            write_tool=write_tool,
        )

    def _activate(session_id: str, plan_id: str) -> dict:
        """激活计划 + 把会话的工作流版本切到 DAG（两件事必须一起发生）。"""
        detail = repository.activate_plan(session_id, plan_id)
        repository.set_workflow_version(session_id, RESEARCH_PLAN_WORKFLOW)
        return detail

    @app.post("/tasks/{session_id}/plans/{plan_id}/activate", response_model=schemas.PlanDetailView)
    def activate_plan(session_id: str, plan_id: str, payload: schemas.RequestMeta):
        runtime.get_state(session_id)
        try:
            detail = _activate(session_id, plan_id)
        except KeyError as exc:
            raise NotFound(str(exc), request_id=payload.request_id)
        except ValueError as exc:
            raise StateConflict(str(exc), request_id=payload.request_id)
        return _detail_view(session_id, detail)

    @app.post("/tasks/{session_id}/plans/{plan_id}/advance",
              response_model=schemas.PlanAdvanceView)
    def advance_plan(session_id: str, plan_id: str, payload: schemas.PlanAdvanceRequest):
        """推进已激活的计划：串行跑有限步，每步都落库；崩溃后重进只补未提交的那步。"""
        if orchestrate_llm is None:
            raise Unrecoverable(
                "本部署未配置模型（启动参数或 LLM_API_KEY/LLM_MODEL）",
                request_id=payload.request_id,
            )
        runtime.get_state(session_id)
        try:
            detail = repository.plan_detail(session_id, plan_id)
        except KeyError as exc:
            raise NotFound(str(exc), request_id=payload.request_id)
        if detail["plan"]["status"] != "active":
            raise StateConflict(
                f"计划 {plan_id} 当前状态 {detail['plan']['status']}，先激活再推进",
                request_id=payload.request_id,
            )

        executor = _build_executor(session_id, detail["plan"]["summary"])
        runner = PlanRunner(repository, session_id, plan_id, executor,
                            max_ticks=max(2, payload.max_steps * 4))
        report = runner.run()
        _sync_task_state(runtime, session_id, report)
        after = repository.plan_detail(session_id, plan_id)
        state = runtime.get_state(session_id)
        return schemas.PlanAdvanceView(
            session_id=session_id,
            plan_id=plan_id,
            state=state["state"],
            workflow_version=state.get("workflow_version", ""),
            executed=list(report.executed),
            needs_human=list(report.needs_human),
            failed=list(report.failed),
            recovered_in_flight=list(report.recovered_in_flight),
            revisions=list(report.revisions),
            stopped_reason=report.stopped_reason,
            complete=report.complete,
            steps=_step_views(after),
        )

    @app.post("/tasks/{session_id}/plans/{plan_id}/recover",
              response_model=schemas.PlanRecoverView)
    def recover_plan(session_id: str, plan_id: str, payload: schemas.PlanRecoverRequest):
        """按持久化状态恢复：先评估能不能接着跑，再决定要不要动手。

        `dry_run=true` 只回答"卡在哪"；否则在**没有在途未知调用**的前提下续跑，
        并且只补未提交的那一步——已完成的 Step 不会重跑。
        """
        runtime.get_state(session_id)
        try:
            detail = repository.plan_detail(session_id, plan_id)
        except KeyError as exc:
            raise NotFound(str(exc), request_id=payload.request_id)
        if detail["plan"]["status"] != "active":
            raise StateConflict(
                f"计划 {plan_id} 当前状态 {detail['plan']['status']}，只有 active 计划能恢复",
                request_id=payload.request_id,
            )

        # 对账完成后，人工可以点名重开 needs_human 的步骤（needs_human → pending 是合法转移）。
        # 不自动重开：那等于替人拍板"这个未知已经解决了"。
        reopened: List[str] = []
        for step_id in payload.reopen_steps:
            current = repository.step_statuses(session_id, plan_id).get(step_id, "")
            if current != "needs_human":
                continue
            repository.set_step_status(
                session_id, plan_id, step_id, "pending",
                reason="人工指定重开（对账已定论）", actor="recover",
            )
            reopened.append(step_id)

        status = assess_recovery(repository, session_id)
        if status.plan_id != plan_id:
            # 传进来的不是当前激活计划：评估结果对不上，明确拒绝而不是混着报
            raise StateConflict(
                f"计划 {plan_id} 不是当前激活计划（当前是 {status.plan_id or '无'}）",
                request_id=payload.request_id,
            )
        result = schemas.PlanRecoverView(**status.to_dict(),
                                         reopened=reopened,
                                         state=runtime.get_state(session_id)["state"])
        if payload.dry_run or status.blocked:
            return result

        if orchestrate_llm is None:
            raise Unrecoverable(
                "本部署未配置模型（启动参数或 LLM_API_KEY/LLM_MODEL）",
                request_id=payload.request_id,
            )
        executor = _build_executor(session_id, detail["plan"]["summary"])
        report = PlanRunner(repository, session_id, plan_id, executor).run()
        _sync_task_state(runtime, session_id, report)
        return schemas.PlanRecoverView(
            **status.to_dict(),
            reopened=reopened,
            executed=list(report.executed),
            recovered_in_flight=list(report.recovered_in_flight),
            revisions=list(report.revisions),
            stopped_reason=report.stopped_reason,
            complete=report.complete,
            state=runtime.get_state(session_id)["state"],
        )

    def _sync_task_state(runtime_: Any, session_id: str, report) -> None:
        """把计划推进结果同步到任务生命周期：收敛或明确停住，不悬在中间态。"""
        state = runtime_.get_state(session_id)["state"]
        if state in terminal_states:
            return
        try:
            if report.complete and state == "paused":
                # 之前因为 needs_human 暂停过；现在计划跑完了，先把任务恢复再收敛
                runtime_.resume_task(session_id)
                state = runtime_.get_state(session_id)["state"]
            if report.complete:
                for trigger, expected in (("plan_ready", "planning"),
                                          ("work_done", "executing"),
                                          ("audit_pass", "auditing")):
                    if runtime_.get_state(session_id)["state"] != expected:
                        continue
                    runtime_.advance_task(session_id, trigger, reason="DAG 计划完成")
            elif report.stopped_reason == "needs_human":
                runtime_.pause_task(session_id, reason="计划里有步骤需要人工确认")
            elif report.stopped_reason == "failed":
                runtime_.advance_task(session_id, "fail", reason="计划推进失败")
        except RuntimeFailure:
            pass

    # ---------- 工具调用与对账 ----------

    @app.get("/tasks/{session_id}/tool-calls", response_model=schemas.ToolCallListView)
    def list_tool_calls(session_id: str, status: str = "", plan_id: str = ""):
        runtime.get_state(session_id)
        rows = repository.tool_calls(session_id, status=status, plan_id=plan_id)
        return schemas.ToolCallListView(
            session_id=session_id,
            tool_calls=[schemas.ToolCallView(**item) for item in rows],
        )

    @app.post("/tasks/{session_id}/tool-calls/{tool_call_id}/reconcile",
              response_model=schemas.ToolCallView)
    def reconcile_tool_call(session_id: str, tool_call_id: str,
                            payload: schemas.ReconcileRequest):
        """对账：能给出明确结论就落结论，给不出就留在 indeterminate（不猜）。"""
        runtime.get_state(session_id)
        try:
            record = repository.get_tool_call(session_id, tool_call_id)
        except KeyError as exc:
            raise NotFound(str(exc), request_id=payload.request_id)
        if record["status"] == "dispatched":
            # 派发之后进程挂了：没有结论，先按"结果未知"落定，再走对账
            record = repository.settle_tool_call(
                session_id, tool_call_id, status="indeterminate",
                outcome="unknowable",
                error={"kind": "unknown_commit",
                       "message": "已派发但未收到结论，进入对账"},
            )
        if record["status"] != "indeterminate":
            raise StateConflict(
                f"工具调用当前状态 {record['status']}，只有 dispatched/indeterminate 需要对账",
                request_id=payload.request_id,
            )

        outcome = (payload.outcome or "").strip().lower()
        detail = payload.detail
        if not outcome:
            # 没给结论：用观测判定；判不了就如实记 unknowable
            from atlas.tools.calls import decide_from_observation

            decision = decide_from_observation(payload.observed, expected=payload.expected)
            outcome = (decision.outcome.value if decision.outcome
                       else "unknowable")
            detail = detail or decision.detail
        if outcome not in ("applied", "not_applied", "partially_applied", "unknowable"):
            raise InvalidRequest(f"未知对账结论：{outcome}", request_id=payload.request_id)

        repository.begin_reconcile(session_id, tool_call_id)
        updated = repository.finish_reconcile(
            session_id, tool_call_id, outcome=outcome, detail=detail,
            result=payload.result,
        )
        return schemas.ToolCallView(**updated)

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
