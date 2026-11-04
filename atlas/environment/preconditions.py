"""前置/后置条件：执行**之前**复查外部世界，执行**之后**读回比对（设计文档 §4.9 / §5.5）。

为什么前置条件重要：plan 是在某一刻对世界做出的断言。等到真要派发时，世界可能已经变了。
不做复查，就只能等 postcondition 读回才发现——那时候**副作用已经发出去了**。

所以这里给三态结论，与项目一贯的纪律一致：

    passed   —— 条件满足，可以派发
    violated —— 条件不满足，**不许派发**（世界已经不是你计划时的样子）
    unknown  —— 观察过期或没观察过，**判不了** ⇒ 转人工，绝不当成满足

`unknown` 那一档是这套东西能成立的关键：把"没看到"当成"没问题"，等于给重复副作用开门。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from atlas.environment.observations import (
    EnvironmentSnapshot,
    Observation,
    resource_fingerprint,
)

#: 支持的条件类型（够用就好，不做通用表达式语言）。
PRECONDITION_KINDS = ("exists", "absent", "equals", "fingerprint", "contains")


@dataclass(frozen=True)
class Precondition:
    """一条前置条件：对某个对象，要求它处于某种状态。"""

    target: str
    kind: str = "absent"
    value: Any = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.kind not in PRECONDITION_KINDS:
            raise ValueError(f"未知前置条件类型 {self.kind!r}（可选 {PRECONDITION_KINDS}）")
        if not str(self.target or "").strip():
            raise ValueError("Precondition.target 不能为空")

    @classmethod
    def from_dict(cls, payload: Optional[Mapping[str, Any]]) -> Optional["Precondition"]:
        if not payload:
            return None
        return cls(
            target=str(payload.get("target") or ""),
            kind=str(payload.get("kind") or "absent"),
            value=payload.get("value"),
            fingerprint=str(payload.get("fingerprint") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"target": self.target, "kind": self.kind,
                "value": self.value, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class PrecheckResult:
    passed: Optional[bool]      # True / False / None（判不了）
    reason: str = ""
    observed_fingerprint: str = ""
    observe_needed: bool = False

    @property
    def unknown(self) -> bool:
        return self.passed is None

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason,
                "observed_fingerprint": self.observed_fingerprint,
                "observe_needed": self.observe_needed}


def _unknown(reason: str, *, observe_needed: bool = True) -> PrecheckResult:
    return PrecheckResult(None, reason, observe_needed=observe_needed)


def check_precondition(
    precondition: Optional[Precondition],
    snapshot: EnvironmentSnapshot,
    *,
    now: Optional[float] = None,
    ttl_s: float = 60.0,
) -> PrecheckResult:
    """按快照判断前置条件；快照里没有或过期 ⇒ `unknown`（不是"满足"）。"""
    if precondition is None:
        return PrecheckResult(True, "没有前置条件")

    observation = snapshot.get(precondition.target)
    if observation is None:
        return _unknown(f"没有对 {precondition.target} 的观察")
    if not observation.is_fresh(now, ttl_s):
        return _unknown(
            f"对 {precondition.target} 的观察已过期（{observation.age_s:.0f}s），"
            "过期不等于没变，需要重新观察",
            observe_needed=True,
        )

    kind = precondition.kind
    if kind == "exists":
        if observation.present:
            return PrecheckResult(True, f"{precondition.target} 存在",
                                  observation.fingerprint)
        return PrecheckResult(False, f"{precondition.target} 不存在",
                              observation.fingerprint)
    if kind == "absent":
        if not observation.present:
            return PrecheckResult(True, f"{precondition.target} 尚不存在（可以创建）",
                                  observation.fingerprint)
        return PrecheckResult(False, f"{precondition.target} 已经存在，不能重复创建",
                              observation.fingerprint)
    if kind == "equals":
        expected = precondition.value
        actual = observation.value
        if actual == expected:
            return PrecheckResult(True, "观测与期望一致", observation.fingerprint)
        return PrecheckResult(
            False, f"观测与期望不一致（期望 {expected!r}，实际 {actual!r}）",
            observation.fingerprint)
    if kind == "fingerprint":
        if precondition.fingerprint and observation.fingerprint == precondition.fingerprint:
            return PrecheckResult(True, "资源指纹未变", observation.fingerprint)
        return PrecheckResult(
            False, f"资源指纹已变（计划时 {precondition.fingerprint or '未记录'}，"
                   f"现在 {observation.fingerprint}）", observation.fingerprint)
    if kind == "contains":
        expected = precondition.value or {}
        if not isinstance(observation.value, Mapping):
            return PrecheckResult(False, "观测到的不是对象，无法比较字段",
                                  observation.fingerprint)
        mismatched = [key for key, value in dict(expected).items()
                      if observation.value.get(key) != value]
        if not mismatched:
            return PrecheckResult(True, "期望字段都在", observation.fingerprint)
        return PrecheckResult(False, f"字段不一致：{mismatched}",
                              observation.fingerprint)
    return _unknown(f"未知条件类型 {kind}")


@dataclass(frozen=True)
class PostconditionResult:
    """执行之后读回比对的结论。"""

    confirmed: Optional[bool]
    reason: str = ""
    expected_fingerprint: str = ""
    observed_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"confirmed": self.confirmed, "reason": self.reason,
                "expected_fingerprint": self.expected_fingerprint,
                "observed_fingerprint": self.observed_fingerprint}


def check_postcondition(expected: Any, observed: Any, *,
                        strict: bool = False) -> PostconditionResult:
    """读回比对：**默认按子集**（服务端常会补字段），`strict` 时才要求逐字段相同。

    子集是刻意的：真实服务会在对象上加 `updated_at`、`version` 之类的字段，
    逐字段相等会让"其实写成功了"被判成失败——那就变成了假阴性。
    但**缺字段**一定要算失败：少写了一个字段就是没写全。
    """
    if not isinstance(observed, Mapping):
        return PostconditionResult(False, "读回的不是对象")
    if not isinstance(expected, Mapping):
        return PostconditionResult(False, "预期值不是对象")
    expected_fp = resource_fingerprint(dict(expected))
    observed_fp = resource_fingerprint(dict(observed))
    if strict:
        if dict(expected) == dict(observed):
            return PostconditionResult(True, "读回与预期逐字段一致",
                                       expected_fp, observed_fp)
        return PostconditionResult(False, "读回与预期不一致", expected_fp, observed_fp)
    missing = [key for key in expected if key not in observed]
    if missing:
        return PostconditionResult(False, f"读回缺少字段 {missing}", expected_fp, observed_fp)
    mismatched = [key for key, value in expected.items()
                  if observed.get(key) != value]
    if mismatched:
        return PostconditionResult(False, f"读回字段值不一致 {mismatched}",
                                   expected_fp, observed_fp)
    return PostconditionResult(True, "读回包含全部期望字段", expected_fp, observed_fp)


def preconditions_from_payload(payload: Optional[Mapping[str, Any]]) -> List[Precondition]:
    """从 step 的 `tool_args_template` 里读前置条件（列表或单条都行）。"""
    if not payload:
        return []
    raw = payload.get("preconditions")
    if raw is None:
        single = Precondition.from_dict(payload.get("precondition"))
        return [single] if single else []
    if isinstance(raw, Mapping):
        raw = [raw]
    items: List[Precondition] = []
    for entry in raw:
        parsed = Precondition.from_dict(entry) if isinstance(entry, Mapping) else None
        if parsed is not None:
            items.append(parsed)
    return items


def observations_for(targets: Any, *, source: str = "readback") -> EnvironmentSnapshot:
    """把"读回结果"折成快照；`targets` 是 `{target: value 或 None}`。"""
    entries = []
    for target, value in dict(targets or {}).items():
        if value is None:
            entries.append(Observation.absent(str(target), source=source))
        else:
            entries.append(Observation(target=str(target), value=value, source=source))
    return EnvironmentSnapshot.of(entries)
