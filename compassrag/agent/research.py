"""ResearchResult 适配层：内部运行 → 对外契约（阶段 A 的 ResearchResult、阶段 E 的本地 adapter）。

Atlas 只应该看到"答案 + 哪几段原文支持它 + 为什么停 + 失败属于哪一类"，不应该看到
`ToolHit`、`AgentState` 或索引内部结构。这层就负责这次翻译，规则全部是确定性的：

* 证据按 `AnswerTrace.evidence_ids` 的顺序取，`retriever`/`rank`/`score` 从当次工具调用里回填；
* `quote` 截断到固定长度（协议响应不该塞整段文本）；
* `failure` 把内部 `StopReason` 映射成 Atlas 能分支处理的五类；
* `confidence` 是**启发式**（证据数量 × claim 支持率 × 停止原因），不是校准过的概率，
  只用于让上游决定是否复核——文档与简历都必须这么写。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .core import Agent, AgentResult
from .graph import GraphRunner
from .schemas import (
    AnswerTrace,
    ResearchEvidence,
    ResearchRequest,
    ResearchResult,
    StopReason,
    ToolHit,
)

QUOTE_CHARS_DEFAULT = 280
ANSWERLESS = {"", "unknown", "none", "n/a", "i don't know", "not known"}
EVIDENCE_LIMIT_DEFAULT = 5

_FAILURE_BY_STOP: dict[StopReason, str] = {
    StopReason.EMPTY_EVIDENCE: "no_evidence",
    StopReason.INSUFFICIENT_EVIDENCE: "no_evidence",
    StopReason.TOOL_FAILURE: "upstream_error",
    StopReason.ERROR: "upstream_error",
}


def is_answerless(answer: str) -> bool:
    """模型回了 empty/unknown 之类，等于没答出来——对外要当成失败信号，而不是成功。"""
    return answer.strip().strip(".").lower() in ANSWERLESS


def failure_category(stop_reason: str) -> str:
    """内部停止原因 → Atlas 的五类失败（成功返回空串）。"""
    try:
        reason = StopReason(stop_reason)
    except ValueError:
        return "upstream_error"
    if reason.is_budget:
        return "budget_exhausted"
    return _FAILURE_BY_STOP.get(reason, "")


def _hit_index(trace: AnswerTrace) -> dict[str, ToolHit]:
    """chunk → 首次命中的 ToolHit（保留 retriever/rank/score）。"""
    out: dict[str, ToolHit] = {}
    for call in trace.tool_calls:
        for hit in call.hits:
            prev = out.get(hit.chunk_id)
            if prev is None or hit.rank < prev.rank:
                out[hit.chunk_id] = hit
    return out


def evidence_from_trace(trace: AnswerTrace, store: dict[str, dict],
                        *, limit: int = EVIDENCE_LIMIT_DEFAULT,
                        quote_chars: int = QUOTE_CHARS_DEFAULT) -> list[ResearchEvidence]:
    hits = _hit_index(trace)
    out: list[ResearchEvidence] = []
    for cid in trace.evidence_ids[:limit]:
        chunk = store.get(cid)
        if chunk is None:
            continue
        hit = hits.get(cid)
        text = str(chunk.get("text", ""))
        out.append(ResearchEvidence(
            source=str(chunk.get("title", "")),
            chunk_id=cid,
            quote=text[:quote_chars] + ("…" if len(text) > quote_chars else ""),
            score=float(hit.score) if hit else 0.0,
            retriever=hit.retriever if hit else "",
            rank=hit.rank if hit else 0,
        ))
    return out


def confidence_of(trace: AnswerTrace, evidence: list[ResearchEvidence]) -> float:
    """启发式置信度（不是校准概率）：证据覆盖 × claim 支持率 × 停止原因。"""
    if not evidence or failure_category(trace.stop_reason.value) or is_answerless(trace.answer):
        return 0.0
    coverage = min(1.0, len(evidence) / EVIDENCE_LIMIT_DEFAULT)
    v = trace.verification
    if v is not None and v.parsed and v.claims:
        support = sum(1 for c in v.claims if c.supported) / len(v.claims)
    else:
        support = 0.5   # 没跑核验：不假设支持，也不假设不支持
    stop_ok = 1.0 if trace.stop_reason in (StopReason.ANSWERED, StopReason.EARLY_STOP) else 0.3
    return round(min(1.0, 0.45 * coverage + 0.35 * support + 0.20 * stop_ok), 4)


def to_research_result(result: AgentResult, *, store: dict[str, dict],
                       limit: int = EVIDENCE_LIMIT_DEFAULT,
                       quote_chars: int = QUOTE_CHARS_DEFAULT) -> ResearchResult:
    trace = result.trace
    if trace is None:
        # 没开 capture_trace 时仍然要给结构化结果（证据退化为 id 列表）
        try:
            stop = StopReason(result.stop_reason)
        except ValueError:
            stop = StopReason.ANSWERED
        trace = AnswerTrace(question="", answer=result.answer, stop_reason=stop,
                            evidence_ids=list(result.evidence), rounds=result.rounds,
                            route=result.route)
    evidence = evidence_from_trace(trace, store, limit=limit, quote_chars=quote_chars)
    failure = failure_category(result.stop_reason)
    if not failure and is_answerless(result.answer):
        failure = "no_evidence"        # 有证据但没答出来，对上游同样是"没拿到答案"
    return ResearchResult(
        answer=result.answer,
        evidence=evidence,
        confidence=0.0 if failure else confidence_of(trace, evidence),
        retrieval_rounds=result.rounds,
        stop_reason=result.stop_reason,
        trace_id=trace.run_id,
        failure=failure,
    )


def research(agent: Agent, request: ResearchRequest, *,
             cache: dict[str, ResearchResult] | None = None,
             on_trace=None,
             use_graph: bool = True,
             run_id: str = "",
             on_event=None,
             cancel_check=None,
             ) -> ResearchResult:
    """本地 adapter：按请求预算跑一次，返回对外契约。

    `idempotency_key` 命中缓存时直接返回上一次结果（阶段 E 的幂等语义）。
    """
    if cache is not None and request.idempotency_key:
        if request.idempotency_key in cache:
            return cache[request.idempotency_key]

    cfg = dataclasses.replace(
        agent.config,
        max_rounds=request.max_rounds,
        max_total_tokens=request.budget_tokens or agent.config.max_total_tokens,
    )
    runner = Agent(agent.llm, agent.tools, agent.store, cfg)
    # 阶段 C：对外调用统一走 graph runner；`use_graph=False` 只留给对照/回退
    run_kwargs = {"run_id": run_id, "on_event": on_event, "cancel_check": cancel_check}
    result = (GraphRunner(runner).run(request.query, **run_kwargs) if use_graph
              else runner.answer(request.query, **run_kwargs))
    if on_trace is not None and result.trace is not None:
        on_trace(result.trace)
    out = to_research_result(result, store=agent.store)
    if cache is not None and request.idempotency_key:
        cache[request.idempotency_key] = out
    return out
