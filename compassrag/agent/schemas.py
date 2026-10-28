"""Agent 层数据契约（Pydantic v2）。

设计原则（与项目方法论一致）：**LLM 只负责产出这些结构的候选值，校验、执行、记录
由确定性代码完成。** 任何解析失败都降级为安全值（single / sufficient），不允许决策
组件把整题拖死。

这些模型同时是 `eval/traces.py` 的落盘格式：一题一份 `AnswerTrace`，据此可以在不重新
请求模型的情况下重算证据覆盖与停止原因。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# 路由 / 查询来源 / 停止原因
# --------------------------------------------------------------------------- #


class Route(str, Enum):
    """问题难度路由：直答 / 单跳 / 多跳。"""

    DIRECT = "direct"
    SINGLE = "single"
    MULTI = "multi"


class QuerySource(str, Enum):
    """每条检索查询的来源——回答"这条查询为什么会存在"。"""

    ORIGINAL = "original"        # 原问题
    SUBQUESTION = "subquestion"  # planner 拆出的子问题
    SUFFICIENCY = "sufficiency"  # 充分度判定生成的下一条查询
    HYDE = "hyde"                # HyDE 假答案改写
    REWRITE = "rewrite"          # 其他改写变体
    REPAIR = "repair"            # 证据闭环触发的补检索


class StopReason(str, Enum):
    """为什么停在这里。评测与面试都必须能回答这一个字段。"""

    DIRECT = "direct"                    # direct 路由，未检索
    ANSWERED = "answered"                # 正常作答（预算内自然结束）
    EARLY_STOP = "early_stop"            # 充分度判定 ok，提前收手
    BUDGET_ROUNDS = "budget_exhausted:rounds"
    BUDGET_TOKENS = "budget_exhausted:tokens"
    BUDGET_LATENCY = "budget_exhausted:latency"
    BUDGET_COST = "budget_exhausted:cost"
    EMPTY_EVIDENCE = "empty_evidence"    # 全部工具都空结果
    TOOL_FAILURE = "tool_failure"        # 工具连续报错，无法继续
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # 证据不支持结论且修复已用尽 → 拒答
    ERROR = "error"                      # 未预期异常（评测需要把这类题单独计数）
    CANCELLED = "cancelled"              # 客户端主动取消（不再发起新的模型/检索调用）

    @property
    def is_budget(self) -> bool:
        return self.value.startswith("budget_exhausted:")


# --------------------------------------------------------------------------- #
# 检索请求 / 工具调用
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    """统一工具协议的输入：**所有**检索工具都吃这一个结构。"""

    model_config = ConfigDict(frozen=True)

    query: str
    top_k: int = Field(default=10, ge=1, le=500)
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def _non_empty_query(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query 不能为空")
        return v

    def input_hash(self) -> str:
        """同一次检索的稳定指纹，用于跨 run 对齐与去重。"""
        payload = json.dumps(
            {"q": self.query, "k": self.top_k, "f": self.filters},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class ToolHit(BaseModel):
    """统一工具协议的输出项。score 的语义随 retriever 变（BM25 原始分 / 余弦 / PPR），
    **不做跨工具可比假设**——融合只用 rank。"""

    chunk_id: str
    score: float
    retriever: str = ""
    rank: int = 0


ToolOutcome = Literal["ok", "empty", "error", "timeout", "invalid_params", "index_missing"]


class ToolCall(BaseModel):
    """一次工具调用的完整记录（轨迹里可回放的最小单元）。"""

    step_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_step_id: str = ""
    tool_name: str
    request: SearchRequest
    hits: list[ToolHit] = Field(default_factory=list)
    outcome: ToolOutcome = "ok"
    error: str = ""
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def chunk_ids(self) -> list[str]:
        return [h.chunk_id for h in self.hits]


# --------------------------------------------------------------------------- #
# 决策组件的输出
# --------------------------------------------------------------------------- #


class RouteDecision(BaseModel):
    """路由器输出。`parsed=False` 表示模型输出不合法、已降级。"""

    route: Route = Route.SINGLE
    subquestions: list[str] = Field(default_factory=list)
    parsed: bool = True
    fallback_reason: str = ""


class SearchPlan(BaseModel):
    """一次检索计划：路由 + 要查的查询（含来源）。"""

    route: Route = Route.SINGLE
    queries: list[str] = Field(default_factory=list)
    parsed: bool = True


class EvidenceCheck(BaseModel):
    """充分度判定输出：够不够答、不够就给出下一条查询。"""

    sufficient: bool = True
    next_query: str = ""
    missing: list[str] = Field(default_factory=list)
    parsed: bool = True


class Claim(BaseModel):
    """从答案里拆出的一条待核验断言。`evidence_ids` 是**代码校验过**的引用。"""

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    supported: bool = False
    reason: str = ""


class VerificationResult(BaseModel):
    """claim 级核验结果。parsed=False 表示模型输出不合法（此时不触发修复，只记降级）。"""

    claims: list[Claim] = Field(default_factory=list)
    parsed: bool = True
    source_conflict: bool = False
    conflict_note: str = ""

    @property
    def unsupported(self) -> int:
        return sum(1 for c in self.claims if not c.supported)

    @property
    def all_supported(self) -> bool:
        return bool(self.claims) and self.unsupported == 0

    @property
    def needs_repair(self) -> bool:
        """有断言无支撑，或证据源互相冲突——都要求回到检索。"""
        return self.parsed and (not self.claims or self.unsupported > 0 or self.source_conflict)


class FailureClass(str, Enum):
    """失败五分法。前三类由**规则**判定（便宜、可复现），后两类靠模型核验。"""

    QUERY_MISS = "query_miss"            # 某轮检索没带来任何新证据
    EVIDENCE_GAP = "evidence_gap"        # 证据不足以回答，且没有可继续查的方向
    SOURCE_CONFLICT = "source_conflict"  # 两段证据对同一事实给出不同值
    ANSWER_UNSUPPORTED = "answer_unsupported"  # 答案里有断言没有证据支撑
    TOOL_FAILURE = "tool_failure"        # 工具报错/超时/索引缺失


class RepairAction(str, Enum):
    """有限修复动作。每类失败只给一个动作，且全局最多修 `max_repairs` 次。"""

    REWRITE_QUERY = "rewrite_query"
    RETRIEVE_MORE = "retrieve_more"
    SWITCH_VIEW = "switch_view"
    DECOMPOSE = "decompose"
    REFUSE = "refuse"


# --------------------------------------------------------------------------- #
# 用量 / 轨迹
# --------------------------------------------------------------------------- #


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, resp: Any) -> "TokenUsage":
        """从 LLMResponse 累积（保持原对象，便于状态共享）。"""
        self.prompt_tokens += int(getattr(resp, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(resp, "completion_tokens", 0) or 0)
        self.reasoning_tokens += int(getattr(resp, "reasoning_tokens", 0) or 0)
        return self

    def add_usage(self, other: "TokenUsage") -> "TokenUsage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        return self


class QueryRecord(BaseModel):
    """一条实际用于检索的查询 + 它带来了多少新证据。"""

    query: str
    source: QuerySource = QuerySource.ORIGINAL
    round_index: int = 0
    new_evidence: int = 0


class TraceStep(BaseModel):
    """轨迹里的一个步骤：plan / retrieve / verify / answer。"""

    step_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_step_id: str = ""
    kind: Literal["plan", "retrieve", "verify", "repair", "answer", "finish"]
    tag: str = ""                      # llm 调用标签（plan / sufficiency / answer …）
    detail: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ResearchEvidence(BaseModel):
    """对外证据项：Atlas 只需要"哪篇文章的哪段原文支持了答案"，不需要内部块结构。"""

    source: str = ""          # article title
    chunk_id: str = ""
    quote: str = ""           # 原文片段（截断，避免把整段塞进协议响应）
    score: float = 0.0
    retriever: str = ""
    rank: int = 0


class ResearchRequest(BaseModel):
    """Atlas 调用 CompassRAG 的请求（阶段 A 契约，阶段 E 由 API 复用）。"""

    session_id: str = ""
    subtask_id: str = ""
    query: str
    budget_tokens: int = Field(default=0, ge=0)
    max_rounds: int = Field(default=2, ge=1)
    idempotency_key: str = ""
    timeout_s: float = Field(default=0.0, ge=0.0)   # 0 = 不设墙钟上限

    @field_validator("query")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query 不能为空")
        return v


# 对外失败分类：Atlas 据此决定重试/暂停/改子任务（与内部 StopReason 是两套词表）
FailureCategory = Literal["", "timeout", "budget_exhausted", "no_evidence",
                          "upstream_error", "invalid_request", "cancelled"]


class ResearchResult(BaseModel):
    """对外响应：答案 + 结构化证据 + 置信度 + 停止原因 + trace 指针。"""

    answer: str = ""
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    retrieval_rounds: int = 0
    stop_reason: str = ""
    trace_id: str = ""
    failure: FailureCategory = ""


class AnswerTrace(BaseModel):
    """一题的完整轨迹——评测的第一等产物，指标行由它派生。"""

    schema_version: int = 1
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    question: str
    route: Route = Route.SINGLE
    stop_reason: StopReason = StopReason.ANSWERED
    answer: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_titles: list[str] = Field(default_factory=list)
    queries: list[QueryRecord] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    steps: list[TraceStep] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    n_llm_calls: int = 0
    n_answer_calls: int = 0
    rounds: int = 0
    latency_ms: float = 0.0
    parse_fallbacks: int = 0
    verification: VerificationResult | None = None
    failures: list[FailureClass] = Field(default_factory=list)
    repairs: list[RepairAction] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    def step(self, kind: str, **kw: Any) -> TraceStep:
        s = TraceStep(kind=kind, **kw)
        self.steps.append(s)
        return s

    def to_row(self) -> dict[str, Any]:
        """扁平化：便于写 jsonl 与聚合。"""
        return json.loads(self.model_dump_json())
