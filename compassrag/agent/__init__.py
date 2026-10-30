"""Agent 包（第 2 代）：数据契约 → 工具协议 → 运行状态 → 控制流。

- `schemas`：Pydantic 契约（路由 / 检索请求 / 工具命中 / 证据判定 / 停止原因 / 轨迹）；
- `tools`：统一检索工具协议，异常不外泄（失败落成 outcome 由控制流处理）；
- `state`：一题一次的累积状态与预算；
- `core`：`plan → (retrieve → verify)* → answer` 控制流，scripted / decision 共用。

导入路径保持兼容：`from compassrag.agent import Agent, AgentConfig, AgentResult`。
"""

from .core import ANSWER_PROMPT, RETRY_NUDGE, Agent, AgentConfig, AgentResult
from .research import (
    confidence_of,
    evidence_from_trace,
    failure_category,
    research,
    to_research_result,
)
from .schemas import (
    AnswerTrace,
    Claim,
    EvidenceCheck,
    FailureClass,
    QueryRecord,
    QuerySource,
    RepairAction,
    Route,
    RouteDecision,
    SearchPlan,
    SearchRequest,
    ResearchEvidence,
    ResearchRequest,
    ResearchResult,
    StopReason,
    TokenUsage,
    VerificationResult,
    ToolCall,
    ToolHit,
    TraceStep,
)
from .state import AgentState, Budget
from .tools import (
    ToolError,
    ToolIndexMissing,
    ToolInvalidParams,
    ToolRegistry,
    ToolSpec,
    ToolTimeout,
    build_registry,
)

__all__ = [
    # core
    "Agent", "AgentConfig", "AgentResult", "ANSWER_PROMPT", "RETRY_NUDGE",
    # schemas
    "AnswerTrace", "TraceStep", "TokenUsage", "QueryRecord", "QuerySource",
    "Claim", "VerificationResult", "FailureClass", "RepairAction",
    "Route", "RouteDecision", "SearchPlan", "SearchRequest", "StopReason",
    "ResearchRequest", "ResearchResult", "ResearchEvidence",
    "ToolCall", "ToolHit", "EvidenceCheck",
    # state
    "AgentState", "Budget",
    # tools
    "ToolSpec", "ToolRegistry", "ToolError", "ToolTimeout",
    "ToolInvalidParams", "ToolIndexMissing", "build_registry",
    # research adapter
    "research", "to_research_result", "evidence_from_trace", "confidence_of",
    "failure_category",
]
