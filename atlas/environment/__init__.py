"""环境状态：观测 / 快照 / 新鲜度 / 前置后置条件（设计文档 §4.9 · Phase 4）。"""

from atlas.environment.observations import (
    EnvChange,
    EnvironmentSnapshot,
    Observation,
    resource_fingerprint,
)
from atlas.environment.preconditions import (
    PRECONDITION_KINDS,
    PostconditionResult,
    PrecheckResult,
    Precondition,
    check_postcondition,
    check_precondition,
    observations_for,
    preconditions_from_payload,
)

__all__ = [
    "EnvChange",
    "EnvironmentSnapshot",
    "Observation",
    "resource_fingerprint",
    "PRECONDITION_KINDS",
    "PostconditionResult",
    "PrecheckResult",
    "Precondition",
    "check_postcondition",
    "check_precondition",
    "observations_for",
    "preconditions_from_payload",
]
