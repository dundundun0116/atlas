"""研究工具层（阶段 A）：协议、CompassRAG 适配器。"""

from atlas.tools.compassrag import (
    CompassRAGAgentTool,
    CompassRAGLocalTool,
    HttpCompassRAGTool,
    LocalResearchTool,
    describe_failure,
)
from atlas.tools.protocol import (
    ERROR_KINDS,
    EvidenceItem,
    ResearchRequest,
    ResearchResult,
    ResearchTool,
    ResearchToolError,
    build_idempotency_key,
    new_trace_id,
)

__all__ = [
    "ERROR_KINDS",
    "CompassRAGAgentTool",
    "CompassRAGLocalTool",
    "EvidenceItem",
    "HttpCompassRAGTool",
    "LocalResearchTool",
    "ResearchRequest",
    "ResearchResult",
    "ResearchTool",
    "ResearchToolError",
    "build_idempotency_key",
    "describe_failure",
    "new_trace_id",
]
