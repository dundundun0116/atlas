"""一次运行的累积状态与预算（M1/M2 的落点）。

这一层刻意**不依赖任何编排框架**：它只是"这题已经走到哪了"的显式记录。若后续引入
LangGraph，节点读写的就是这个对象，框架负责跳转而不是状态。

预算的语义（违反其一就必须收敛并写清 `StopReason`）：

* `max_rounds`——检索轮数上限，与旧 `AgentConfig.max_rounds` 同义；
* `max_total_tokens`——全部 LLM 调用（plan + sufficiency + answer + retry）累计；
* `max_latency_ms`——本题墙钟时间；
* `max_cost_usd`——按单价换算的费用（单价为 0 时该项不生效）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from pydantic import BaseModel, Field

from .schemas import (
    AnswerTrace,
    QueryRecord,
    QuerySource,
    StopReason,
    TokenUsage,
    ToolCall,
)


class Budget(BaseModel):
    """本题的硬上限。0 / 空表示该项不限制。"""

    max_rounds: int = Field(default=1, ge=1)
    max_total_tokens: int = Field(default=0, ge=0)
    max_latency_ms: float = Field(default=0.0, ge=0.0)
    max_cost_usd: float = Field(default=0.0, ge=0.0)
    price_per_1k_prompt: float = Field(default=0.0, ge=0.0)
    price_per_1k_completion: float = Field(default=0.0, ge=0.0)

    def cost_usd(self, usage: TokenUsage) -> float:
        return (usage.prompt_tokens / 1000.0) * self.price_per_1k_prompt + \
               (usage.completion_tokens / 1000.0) * self.price_per_1k_completion

    def exhausted(self, *, usage: TokenUsage, rounds: int, latency_ms: float) -> StopReason | None:
        """返回触顶原因；未触顶返回 None。顺序即优先级。"""
        if usage.total and self.max_total_tokens and usage.total >= self.max_total_tokens:
            return StopReason.BUDGET_TOKENS
        if self.max_latency_ms and latency_ms >= self.max_latency_ms:
            return StopReason.BUDGET_LATENCY
        if self.max_cost_usd and self.cost_usd(usage) >= self.max_cost_usd:
            return StopReason.BUDGET_COST
        return None


class AgentState:
    """一题一次的运行状态；`trace` 是它的可落盘投影。"""

    def __init__(self, question: str, *, budget: Budget | None = None,
                 config: dict[str, Any] | None = None, run_id: str = "",
                 on_event: Callable[[dict], None] | None = None,
                 cancel_check: Callable[[], bool] | None = None) -> None:
        self.question = question
        self.budget = budget or Budget()
        self.started_at = time.perf_counter()
        self.trace = AnswerTrace(question=question, config=dict(config or {}),
                                 run_id=run_id or uuid.uuid4().hex[:12])
        self.on_event = on_event            # 结构化事件出口（SSE / 日志）
        self.cancel_check = cancel_check    # 协作式取消：每次节点入口检查
        self.cancelled = False
        self.route = None                    # 由控制流写入（direct 路由要用常识提示词）
        self._last_step_ts = time.time()
        self.evidence: list[str] = []
        self.seen: set[str] = set()
        self.rounds = 0
        self.usage = TokenUsage()
        self.sufficiency_stuck = False   # 判定"不够"却没给出下一条查询
        self.next_query = ""             # 判定器最后一次给出的查询
        self.repairs_used = 0            # 已执行修复次数（硬上限见 RepairPlan）
        self.failures: list[ToolCall] = []
        self._pending_records: dict[str, QueryRecord] = {}

    # -- 证据 --------------------------------------------------------------- #

    def add_evidence(self, chunk_ids: list[str]) -> int:
        """按首见顺序累积去重；返回新增条数（query miss 判定的原料）。"""
        new = 0
        for cid in chunk_ids:
            if cid in self.seen:
                continue
            self.seen.add(cid)
            self.evidence.append(cid)
            new += 1
        return new

    def truncate_evidence(self, limit: int) -> None:
        if limit and len(self.evidence) > limit:
            self.evidence = self.evidence[:limit]
            self.seen = set(self.evidence)

    # -- 查询 / 工具 / LLM --------------------------------------------------- #

    def record_query(self, query: str, source: QuerySource = QuerySource.ORIGINAL,
                     round_index: int | None = None) -> QueryRecord:
        rec = QueryRecord(query=query, source=source,
                          round_index=self.rounds if round_index is None else round_index)
        self.trace.queries.append(rec)
        self._pending_records[query] = rec
        return rec

    def mark_new_evidence(self, query: str, new: int) -> None:
        rec = self._pending_records.get(query)
        if rec is not None:
            rec.new_evidence += int(new)

    def add_tool_call(self, call: ToolCall) -> None:
        self.trace.tool_calls.append(call)
        if not call.ok:
            self.failures.append(call)

    def add_llm_call(self, resp: Any) -> None:
        """累积一次 LLM 调用的用量与调用数（步骤由控制流显式登记）。"""
        self.usage.add(resp)
        self.trace.usage = self.usage
        self.trace.n_llm_calls += 1

    # -- 事件 / 取消 --------------------------------------------------------- #

    def step(self, kind: str, **kw: Any):
        """登记一个轨迹步骤，同时把结构化事件推给订阅者。

        事件是**从既有轨迹派生**的，不额外发明状态：`AnswerTrace` 仍是唯一真源，
        SSE 只是它的实时投影。
        """
        step = self.trace.step(kind, **kw)
        now = time.time()
        latency_ms = round((now - self._last_step_ts) * 1000.0, 1)
        self._last_step_ts = now
        if self.on_event is not None:
            detail = step.detail or {}
            tools = detail.get("tools")
            if isinstance(tools, list):
                tool = ",".join(str(t.get("tool", t)) if isinstance(t, dict) else str(t)
                                for t in tools)
            else:
                tool = str(detail.get("tool", "") or "")
            outcomes = [str(t.get("outcome", "ok")) for t in tools
                        if isinstance(t, dict)] if isinstance(tools, list) else []
            event_outcome = next((o for o in outcomes if o != "ok"), "ok")
            self.on_event({
                "kind": kind,
                "step_id": step.step_id,
                "parent_step_id": step.parent_step_id,
                "tag": step.tag,
                "tool": tool,
                "query_source": str(detail.get("source", "") or ""),
                "outcome": event_outcome,
                "detail": detail,
                "round": self.rounds,
                "evidence": len(self.evidence),
                "tokens": self.usage.total,
                "latency_ms": latency_ms,
                "ts": round(now, 3),
            })
        return step

    def check_cancel(self) -> bool:
        """协作式取消：一旦置位就返回 True，调用方必须停止发起新调用。"""
        if not self.cancelled and self.cancel_check is not None and self.cancel_check():
            self.cancelled = True
        return self.cancelled

    # -- 预算 / 收尾 --------------------------------------------------------- #

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def budget_stop(self) -> StopReason | None:
        return self.budget.exhausted(usage=self.usage, rounds=self.rounds,
                                     latency_ms=self.elapsed_ms())

    def finish(self, *, answer: str, stop_reason: StopReason, route: Any) -> AnswerTrace:
        self.trace.answer = answer
        self.trace.stop_reason = stop_reason
        self.trace.route = route
        self.trace.evidence_ids = list(self.evidence)
        self.trace.usage = self.usage
        self.trace.rounds = self.rounds
        self.trace.latency_ms = round(self.elapsed_ms(), 1)
        return self.trace
