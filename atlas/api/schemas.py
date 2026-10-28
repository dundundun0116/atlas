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
    created_at: str = ""


class TaskStateView(BaseModel):
    session_id: str
    goal: str
    state: str
    nodes: List[NodeView] = Field(default_factory=list)
    edges: List[EdgeView] = Field(default_factory=list)
    checkpoint: Optional[CheckpointView] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)


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
