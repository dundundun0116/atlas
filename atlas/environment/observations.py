"""环境状态：观测、快照、新鲜度与资源指纹（设计文档 §4.9 / §11 Phase 4）。

这一层回答一个此前答不出来的问题：**"我上次看到的那个对象，现在还是那样吗？"**

在此之前，运行时只有"知识"（Fact/Constraint）与"执行状态"（Step/ToolCall），没有
"外部世界现在长什么样"的表示。于是两类错误无法避免：

- **拿着过期观察去做判断**：写之前不复查，等 confirm 读回才发现世界早变了；
- **世界变了却继续用旧结论**：上游结果基于已变的资源，下游却照旧当它有效。

三个概念，缺一不可：

1. **`Observation`**：对某个对象的一次观察——值、**资源指纹**、观察到的时间。
   指纹由内容算出（规范化 JSON 的 sha256），内容没变指纹就不变，改一个字段就变；
   这样"变没变"是**算出来的**，不是靠调用方记得。
2. **`EnvironmentSnapshot`**：同一时刻的一组观察，不可变；更新一个对象得到新快照。
   不可变很要紧——重规划要能回答"当时看到的是哪一版"。
3. **新鲜度**：观察有寿命。过期不等于"没变"，而是**不知道**——按项目一贯的纪律，
   不知道就要转人工，而不是当成有效。

这里只做纯数据与判定，不碰存储；落库与调度在 `atlas.planning.replan`。
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


def resource_fingerprint(value: Any) -> str:
    """资源指纹：键排序 + 紧凑分隔 + 中文不转义 ⇒ 同一内容永远同一指纹。"""
    if isinstance(value, str):
        blob = value
    else:
        try:
            blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Observation:
    """一次观察：看到了什么、指纹是多少、什么时候看到的。"""

    target: str
    value: Any = None
    source: str = "readback"
    observed_at: float = field(default_factory=time.time)
    present: bool = True
    #: 不填就按 value 现算；填了就必须与 value 一致（调用方给错指纹是严重 bug）
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not str(self.target or "").strip():
            raise ValueError("Observation.target 不能为空")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", resource_fingerprint(self.value))

    def is_fresh(self, now: Optional[float] = None, ttl_s: float = 60.0) -> bool:
        """新鲜 = 没超过寿命。`ttl_s<=0` 表示"永不过期"（只在测试或只读场景用）。"""
        if ttl_s <= 0:
            return True
        current = time.time() if now is None else now
        return (current - self.observed_at) <= ttl_s

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.observed_at)

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target, "value": self.value, "source": self.source,
                "observed_at": self.observed_at, "present": self.present,
                "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Observation":
        return cls(
            target=str(payload.get("target") or ""),
            value=payload.get("value"),
            source=str(payload.get("source") or "readback"),
            observed_at=float(payload.get("observed_at") or time.time()),
            present=bool(payload.get("present", True)),
            fingerprint=str(payload.get("fingerprint") or ""),
        )

    @classmethod
    def absent(cls, target: str, *, source: str = "readback",
               observed_at: Optional[float] = None) -> "Observation":
        """明确观察到"这个对象不存在"——与"没观察过"是两件事。"""
        return cls(target=target, value=None, source=source,
                   observed_at=time.time() if observed_at is None else observed_at,
                   present=False)


@dataclass(frozen=True)
class EnvChange:
    """两个快照之间发生的变化。"""

    target: str
    kind: str                  # created / updated / deleted
    before: Optional[str] = None   # 之前指纹
    after: Optional[str] = None    # 之后指纹

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target, "kind": self.kind,
                "before": self.before, "after": self.after}


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """某一时刻的一组观察；不可变，更新一个对象就得到新快照。"""

    observations: Dict[str, Observation] = field(default_factory=dict)

    # ---------- 读 ----------

    def get(self, target: str) -> Optional[Observation]:
        return self.observations.get(target)

    def targets(self) -> Tuple[str, ...]:
        return tuple(sorted(self.observations))

    def fresh(self, *, now: Optional[float] = None,
              ttl_s: float = 60.0) -> "EnvironmentSnapshot":
        return EnvironmentSnapshot({
            target: obs for target, obs in self.observations.items()
            if obs.is_fresh(now, ttl_s)
        })

    def stale_targets(self, *, now: Optional[float] = None,
                      ttl_s: float = 60.0) -> List[str]:
        return sorted(target for target, obs in self.observations.items()
                      if not obs.is_fresh(now, ttl_s))

    # ---------- 写（不可变） ----------

    def observe(self, observation: Observation) -> "EnvironmentSnapshot":
        updated = dict(self.observations)
        updated[observation.target] = observation
        return EnvironmentSnapshot(updated)

    def merge(self, other: "EnvironmentSnapshot") -> "EnvironmentSnapshot":
        updated = dict(self.observations)
        updated.update(other.observations)
        return EnvironmentSnapshot(updated)

    # ---------- 比较 ----------

    def diff(self, other: "EnvironmentSnapshot") -> List[EnvChange]:
        """`self` → `other` 之间变了什么（用于"世界变了要不要重规划"）。"""
        changes: List[EnvChange] = []
        for target in sorted(set(self.observations) | set(other.observations)):
            before = self.observations.get(target)
            after = other.observations.get(target)
            if before is None and after is not None:
                changes.append(EnvChange(target, "created", None, after.fingerprint))
            elif before is not None and after is None:
                changes.append(EnvChange(target, "deleted", before.fingerprint, None))
            elif before is not None and after is not None:
                if before.present != after.present or before.fingerprint != after.fingerprint:
                    changes.append(EnvChange(target, "updated", before.fingerprint,
                                             after.fingerprint))
        return changes

    def to_dict(self) -> Dict[str, Any]:
        return {target: obs.to_dict() for target, obs in sorted(self.observations.items())}

    @classmethod
    def from_dict(cls, payload: Optional[Mapping[str, Any]]) -> "EnvironmentSnapshot":
        return cls({target: Observation.from_dict(item)
                    for target, item in (payload or {}).items()})

    @classmethod
    def of(cls, observations: Iterable[Observation]) -> "EnvironmentSnapshot":
        return cls({obs.target: obs for obs in observations})
