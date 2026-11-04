"""任务 API 的请求/响应契约（阶段 E）。

约定：
- 每个请求都带 `request_id` 与幂等键（`idempotency_key`）；缺省时由服务端补齐，
  但客户端传了就必须原样回显，便于排障。
- 错误体统一为 `{"error": {"type": ..., "message": ..., "request_id": ...}}`，
  `type` 是六种稳定分类之一（见 `ERROR_TYPES`）。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

#: 统一错误分类：参数 / 状态冲突 / 超时 / 存储 / 不可恢复 / 未找到。
ERROR_TYPES = (
    "invalid_request",
    "state_conflict",
    "model_timeout",
    "storage_failure",
    "unrecoverable",
    "not_found",
)


class RequestMeta(BaseModel):
    """所有写请求的公共字段。"""

    request_id: str = Field(default="", description="客户端请求 id；缺省由服务端生成")
    idempotency_key: str = Field(default="", description="同一任务内唯一；重复请求返回原结果")


class CreateTaskRequest(RequestMeta):
    session_id: str = Field(min_length=1, description="任务会话 id")
    goal: str = Field(min_length=1, description="用户总目标")

    @field_validator("session_id", "goal")
    @classmethod
    def _strip(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text


class PauseRequest(RequestMeta):
    """暂停：等待外部输入 / 预算补充 / 人工处理。"""

    reason: str = ""


class AdvanceRequest(RequestMeta):
    trigger: str = Field(min_length=1, description="状态机触发词，如 plan_ready / work_done / audit_pass")
    reason: str = ""


class NoteRequest(RequestMeta):
    """角色写一条结论/产出到共享图（协调全经图，不传对话历史）。"""

    role: str = Field(min_length=1)
    kind: str = Field(default="fact", description="goal/subtask/constraint/decision/artifact/fact")
    content: str = Field(min_length=1)
    reason: str = ""
    evidence: str = ""
    source: str = ""
    confidence: float = 0.0


class ResearchCallRequest(RequestMeta):
    """对当前任务发起一次研究检索（Atlas 侧只负责调用 ResearchTool 并写 Fact）。"""

    query: str = Field(min_length=1)
    subtask_id: str = "S1"
    budget_tokens: int = Field(default=4000, ge=1)
    max_rounds: int = Field(default=2, ge=1)


class OrchestrateRequest(RequestMeta):
    """触发一次完整编排（Planner → Research → Worker → Auditor → 修订）。"""

    problem: str = ""          # 空则用任务目标
    research_query: str = ""   # 空则用 problem
    subtask_id: str = "S1"


class OrchestrateView(BaseModel):
    session_id: str
    state: str
    answer: str = ""
    route: str = ""
    draft: str = ""
    calls: List[str] = Field(default_factory=list)
    fact_ids: List[str] = Field(default_factory=list)
    research_trace_id: str = ""
    research_error: Optional[Dict[str, Any]] = None
    #: 每个模型调用实际使用的上下文指纹（与 /contexts 一一对应）。
    context_digests: List[str] = Field(default_factory=list)
    retries: int = 0
    workflow_version: str = ""


class ResearchCallView(BaseModel):
    session_id: str
    fact_id: str = ""
    answer: str = ""
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    stop_reason: str = ""
    trace_id: str = ""
    failure_kind: str = ""
    failure_message: str = ""


class PlanSubmitRequest(RequestMeta):
    """提交计划候选：给 JSON 就用它，不给就从 problem 生成研究工作流。"""

    problem: str = ""
    plan: Optional[Dict[str, Any]] = None
    with_research: bool = True
    with_repair: bool = True
    activate: bool = False
    budget_tokens: int = Field(default=4000, ge=1)


class PlanAdvanceRequest(RequestMeta):
    """推进已激活的计划：跑有限步（串行），返回这一步之后的状态。"""

    max_steps: int = Field(default=20, ge=1, le=200)


class PlanView(BaseModel):
    plan_id: str
    revision: int = 0
    status: str = ""
    summary: str = ""
    strategy: str = ""
    generated_by: str = ""
    validator_version: str = ""
    created_at: str = ""
    activated_at: str = ""
    validation: Dict[str, Any] = Field(default_factory=dict)


class StepView(BaseModel):
    step_id: str
    title: str = ""
    kind: str = ""
    status: str = ""
    ordinal: int = 0
    tool_name: str = ""
    attempt_count: int = 0
    status_reason: str = ""
    stale_reason: str = ""


class DependencyView(BaseModel):
    from_step_id: str
    to_step_id: str
    kind: str = ""
    binding: Dict[str, str] = Field(default_factory=dict)


class PlanDetailView(BaseModel):
    session_id: str
    plan: PlanView
    steps: List[StepView] = Field(default_factory=list)
    dependencies: List[DependencyView] = Field(default_factory=list)


class PlanListView(BaseModel):
    session_id: str
    plans: List[PlanView] = Field(default_factory=list)


class PlanAdvanceView(BaseModel):
    session_id: str
    plan_id: str
    state: str = ""
    workflow_version: str = ""
    executed: List[str] = Field(default_factory=list)
    needs_human: List[str] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)
    recovered_in_flight: List[str] = Field(default_factory=list)
    stopped_reason: str = ""
    complete: bool = False
    steps: List[StepView] = Field(default_factory=list)


class ToolCallView(BaseModel):
    """一次外部工具调用：四档结论与对账痕迹。"""

    tool_call_id: str
    tool_name: str = ""
    plan_id: str = ""
    step_id: str = ""
    effect_class: str = ""
    status: str = ""
    outcome: str = ""
    idempotency_key: str = ""
    args_fingerprint: str = ""
    retry_count: int = 0
    reconcile_attempts: int = 0
    compensation_for: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    error: Dict[str, Any] = Field(default_factory=dict)
    dispatched_at: str = ""
    settled_at: str = ""


class ToolCallListView(BaseModel):
    session_id: str
    tool_calls: List[ToolCallView] = Field(default_factory=list)


class ReconcileRequest(RequestMeta):
    """人工/自动对账：给出确定结论，或提供读回观测让系统判定。"""

    outcome: str = ""          # applied / not_applied / partially_applied / unknowable
    observed: Optional[Any] = None
    expected: Optional[Any] = None
    detail: str = ""


class NodeView(BaseModel):
    id: str
    type: str
    status: str
    content: str
    actor: str = ""
    source: str = ""
    confidence: float = 0.0
    valid_until: str = ""
    verified_at: str = ""
    reason: str = ""
    evidence: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)


class EdgeView(BaseModel):
    src: str
    dst: str
    type: str


class CheckpointView(BaseModel):
    task_state: str
    graph_version: int
    pending_role: str
    retry_count: int = 0
    context_digest_version: str = ""
    #: §4.13：恢复要知道哪些 Step 还没收敛、哪些外部调用在途、预算还剩多少
    pending_step_ids: List[str] = Field(default_factory=list)
    in_flight_tool_call_ids: List[str] = Field(default_factory=list)
    budget_snapshot: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class TaskStateView(BaseModel):
    session_id: str
    goal: str
    state: str
    failure_reason: str = ""
    allowed_triggers: List[str] = Field(default_factory=list)
    nodes: List[NodeView] = Field(default_factory=list)
    edges: List[EdgeView] = Field(default_factory=list)
    checkpoint: Optional[CheckpointView] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)
    #: 这个会话跑的是哪套控制流（当前 `/orchestrate` 是 legacy_research_v1，不是 DAG）
    workflow_version: str = ""


class AdvanceView(BaseModel):
    session_id: str
    request_id: str
    state: str
    replayed: bool = False


class TraceEventView(BaseModel):
    trace_id: str
    run_id: str = ""
    step_id: str = ""
    parent_step_id: str = ""
    agent_role: str = ""
    tool_name: str = ""
    model: str = ""
    input_hash: str = ""
    latency_ms: float = 0.0
    token_usage: int = 0
    retries: int = 0
    state_version: int = 0
    outcome: str = "ok"
    stop_reason: str = ""
    created_at: str = ""
    #: 这一次调用使用的上下文指纹（可到 /contexts 下钻到包含/排除了哪些状态）
    context_digest: str = ""
    context_tokens: int = 0
    usage_known: bool = False
    error: str = ""


class ContextPackageView(BaseModel):
    """一次模型调用实际看到的上下文包（不存正文，正文可重放）。"""

    context_id: str
    role: str = ""
    state_version: int = 0
    budget_tokens: int = 0
    actual_tokens: int = 0
    digest: str = ""
    tokenizer: str = ""
    sections: Dict[str, int] = Field(default_factory=dict)
    included_refs: List[str] = Field(default_factory=list)
    excluded_refs: List[Dict[str, str]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    degraded: bool = False
    created_at: str = ""


class ContextPackageListView(BaseModel):
    session_id: str
    packages: List[ContextPackageView] = Field(default_factory=list)


class TraceView(BaseModel):
    session_id: str
    events: List[TraceEventView] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


class ReportView(BaseModel):
    session_id: str
    state: str
    report: str


class HealthView(BaseModel):
    ok: bool
    storage: str
    sessions: int
    schema_version: str = "v1"


class ErrorBody(BaseModel):
    type: str
    message: str
    request_id: str = ""


class ErrorResponse(BaseModel):
    error: ErrorBody
