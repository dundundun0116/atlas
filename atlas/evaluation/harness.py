"""阶段 F 的离线 harness：同一 fixture、同一预算，只换记忆策略与注入故障。

之所以离线：真实端点的吞吐波动约 10 倍，小样本数字会让"策略差异"淹没在噪声里。
harness 里的研究者是**脚本化的**——能不能答对只取决于上下文里有没有它需要的东西，
所以策略差异、故障恢复、指标口径都能反复复现，等真正跑评测时只需换研究者实现。
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from atlas.evaluation.fixtures import Fixture, Subtask
from atlas.evaluation.policies import MemoryPolicy, MemoryState, count_words
from atlas.tools.protocol import ResearchToolError


@dataclass
class Faults:
    """五类故障注入（索引是子任务下标，-1 = 不注入）。"""

    tool_timeout_at: int = -1
    empty_response_at: int = -1
    interrupt_at: int = -1
    duplicate_submit_from: int = -1
    stale_decision_at: int = -1


@dataclass
class StepResult:
    subtask_id: str
    completed: bool
    violation: bool = False
    stale_misuse: bool = False
    revised: bool = False
    context_tokens: int = 0
    answer: str = ""
    error: str = ""
    attempts: int = 1
    model_calls: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "StepResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass
class RunResult:
    task_id: str
    family: str
    policy: str
    completed: bool
    steps: List[StepResult] = field(default_factory=list)
    interrupted: bool = False
    recovered: bool = False
    replayed: bool = False
    terminal_state: str = "completed"
    context_tokens: int = 0
    model_calls: int = 0
    latency_ms: float = 0.0
    latency_source: str = "synthetic_estimate"
    cost_usd: float = 0.0

    # ---------- 汇总口径 ----------

    @property
    def violations(self) -> int:
        return sum(1 for s in self.steps if s.violation)

    @property
    def stale_misuses(self) -> int:
        return sum(1 for s in self.steps if s.stale_misuse)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [s.to_dict() for s in self.steps]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RunResult":
        data = dict(payload)
        data["steps"] = [StepResult.from_dict(s) for s in payload.get("steps", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


_LATEX_MAP = (
    ("\\tau", "τ"), ("\\sigma", "σ"), ("\\gamma", "γ"),
    ("\\le", "≤"), ("\\leq", "≤"), ("\\theta", "θ"),
)


def normalize_text(text: str) -> str:
    """判分归一化：去掉 LaTeX/空白/全角差异，保留语义核心。"""
    result = str(text or "")
    for source, target in _LATEX_MAP:
        result = result.replace(source, target)
    for token in ("^{2}", "^2", "²"):
        result = result.replace(token, "2")
    for token in ("$", "{", "}", "\\"):
        result = result.replace(token, "")
    result = result.replace("　", "").replace(" ", "").replace("\n", "").replace("\t", "")
    return result.lower()


def contains_probe(answer: str, probe: str) -> bool:
    """核心断言是否出现（空 probe 视为无要求）。"""
    needle = normalize_text(probe)
    if not needle:
        return True
    return needle in normalize_text(answer)


def answer_mentions_subject(answer: str, question: str) -> bool:
    """要求答案同时提及问题主体，避免单独复述一个数字被判正确。"""
    normalized_answer = normalize_text(answer)
    raw_question = str(question or "")
    candidates = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}|\d+", raw_question)
    for chunk in re.findall(r"[一-龥]{2,}", raw_question):
        # 中文没有空格分词；用二字短语覆盖“引理/递推式/常数项”等主体，
        # 避免把整句当作一个不可命中的 token。
        candidates.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
    ignored = {"多少", "什么", "如何", "哪个", "取值", "得到", "给出", "最终", "对应"}
    subjects = [
        normalize_text(item)
        for item in candidates
        if normalize_text(item) not in ignored and not item.isdigit()
    ]
    return bool(subjects) and any(subject in normalized_answer for subject in subjects)


class ScriptedResearcher:
    """脚本化研究者：答案取决于「上下文里有没有前序事实 / 约束 / 过期值」。"""

    def __init__(self, fixture: Fixture):
        self.fixture = fixture

    def answer(self, subtask: Subtask, context: str) -> Dict[str, Any]:
        """只依据上下文作答：看不见证据就答"无法确定"。

        这一条是整个离线 harness 的可信基础——脚本化研究者不能"自带答案"，
        否则它测的是 fixture，不是记忆策略。
        """
        lines: List[str] = []
        violation = False
        stale_misuse = False
        constraints_visible = all(c in context for c in self.fixture.constraints)
        if subtask.forbidden_method:
            if constraints_visible:
                lines.append(f"（遵守约束：不{subtask.forbidden_method}）")
            else:
                violation = True
                lines.append(f"（{subtask.forbidden_method}）")

        target = subtask.probe or subtask.required_fact
        evidence_visible = contains_probe(context, target)
        earlier_visible = (not subtask.needs_earlier_fact) or contains_probe(
            context, subtask.needs_earlier_fact
        )
        if evidence_visible and earlier_visible:
            lines.append(subtask.required_fact)
            completed = True
        elif subtask.stale_value and contains_probe(context, subtask.stale_value):
            # 证据缺失但旧值可见：真实模型会拿旧值凑答案（这正是要测的失败模式）
            stale_misuse = True
            lines.append(f"采用旧值 {subtask.stale_value}")
            completed = False
        else:
            lines.append("无法确定：上下文缺少证据")
            completed = False
        return {
            "answer": "\n".join(lines),
            "completed": completed,
            "violation": violation,
            "stale_misuse": stale_misuse,
        }


class ModelResearcher:
    """真实模型研究者（Task 6）：把上下文喂给模型，用**输出内容**判定指标。

    判分口径（与脚本化研究者一致，便于对照）：
    - 完成：回答里出现该子任务的核心断言（`probe`）；
    - 违规：回答里出现被约束禁止的做法；
    - 过期误用：回答里出现旧值，且没有给出新值。
    模型异常/空响应按一次失败计，不重试（重试由上层策略决定，这里保持测量纯粹）。
    """

    def __init__(self, fixture: Fixture, llm, *, max_prompt_chars: int = 8000):
        self.fixture = fixture
        self.llm = llm
        self.max_prompt_chars = max_prompt_chars
        self.calls = 0

    def build_prompt(self, subtask: Subtask, context: str) -> str:
        constraints = "\n".join(f"- {c}" for c in self.fixture.constraints) or "（无）"
        return (
            "你是长程研究 Agent 的执行者。只依据下面的记忆上下文回答问题。\n"
            f"[任务目标] {self.fixture.goal}\n"
            f"[约束]\n{constraints}\n"
            f"[记忆上下文]\n{context[: self.max_prompt_chars]}\n"
            f"[当前子问题] {subtask.question}\n"
            "直接给出结论（一句话）；若上下文信息不足，明确回答“无法确定”。"
        )

    def answer(self, subtask: Subtask, context: str) -> Dict[str, Any]:
        self.calls += 1
        try:
            text = str(self.llm(self.build_prompt(subtask, context)) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 记录失败，不吞掉
            return {"answer": "", "completed": False, "violation": False,
                    "stale_misuse": False, "error": f"{type(exc).__name__}: {exc}"}
        target = subtask.probe or subtask.required_fact
        completed = bool(text) and contains_probe(text, target) and answer_mentions_subject(
            text, subtask.question
        )
        violation = bool(subtask.forbidden_method) and contains_probe(
            text, subtask.forbidden_method
        )
        stale_misuse = bool(subtask.stale_value) and (
            contains_probe(text, subtask.stale_value)
            and not contains_probe(text, target)
        )
        return {"answer": text, "completed": completed, "violation": violation,
                "stale_misuse": stale_misuse, "error": "" if text else "empty_response"}


class ResearchHarness:
    """跑一个 fixture：逐子任务取上下文 → 研究 → 记产出（可注入故障）。"""

    def __init__(self, fixture: Fixture, policy: MemoryPolicy,
                 price_per_1k_tokens: float = 0.002, researcher=None):
        self.fixture = fixture
        self.policy = policy
        # 默认脚本化研究者（离线口径验证）；Task 6 传 ModelResearcher 换真实模型
        self.researcher = researcher if researcher is not None else ScriptedResearcher(fixture)
        self.price_per_1k = price_per_1k_tokens
        self.state = MemoryState(
            records=list(fixture.seed_records),
            summaries=list(fixture.seed_summaries),
            constraints=list(fixture.constraints),
        )
        absorb = getattr(policy, "absorb", None)
        if callable(absorb):
            for record in fixture.seed_records:
                absorb(self.state, record)
        self.steps: List[StepResult] = []
        self.cursor = 0
        self.model_calls = 0
        self.context_tokens = 0
        self._idempotent: Dict[int, StepResult] = {}

    # ---------- 对外入口 ----------

    def run(self, faults: Optional[Faults] = None) -> RunResult:
        faults = faults or Faults()
        if faults.interrupt_at >= 0:
            snapshot = self.run_until(faults.interrupt_at, Faults())
            result = self.resume(snapshot, faults)
            result.interrupted = True
            result.recovered = result.completed
            return result
        return self._run_from(self.cursor, faults)

    def run_until(self, index: int, faults: Optional[Faults] = None) -> Dict[str, Any]:
        """跑到指定子任务之前停下，返回可序列化的快照（模拟进程被杀）。"""
        self._run_from(self.cursor, faults or Faults(), stop_before=index)
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        engine = getattr(self.policy, "engine", None)
        return {
            "task_id": self.fixture.task_id,
            "policy": self.policy.name,
            "cursor": self.cursor,
            "records": list(self.state.records),
            "summaries": list(self.state.summaries),
            "engine": engine.to_snapshot() if engine is not None else None,
            "steps": [s.to_dict() for s in self.steps],
            "model_calls": self.model_calls,
            "context_tokens": self.context_tokens,
            "idempotent": {str(k): v.to_dict() for k, v in self._idempotent.items()},
        }

    def resume(self, snapshot: Dict[str, Any], faults: Optional[Faults] = None) -> RunResult:
        """从快照恢复：策略状态、已完成的步骤、幂等缓存都接回来。"""
        self.cursor = int(snapshot["cursor"])
        self.state.records = list(snapshot.get("records", []))
        self.state.summaries = list(snapshot.get("summaries", []))
        self.steps = [StepResult.from_dict(s) for s in snapshot.get("steps", [])]
        self.model_calls = int(snapshot.get("model_calls", 0))
        self.context_tokens = int(snapshot.get("context_tokens", 0))
        self._idempotent = {
            int(k): StepResult.from_dict(v) for k, v in snapshot.get("idempotent", {}).items()
        }
        if snapshot.get("engine"):
            from atlas.memory.engine import GraphMemoryEngine

            engine = GraphMemoryEngine.from_snapshot(snapshot["engine"])
            self.policy.engine = engine
            self.policy.assembler.engine = engine
        result = self._run_from(self.cursor, faults or Faults())
        return result

    # ---------- 内部 ----------

    def _run_from(self, start: int, faults: Faults, stop_before: int = -1) -> RunResult:
        index = start
        while index < len(self.fixture.subtasks):
            if stop_before >= 0 and index >= stop_before:
                break
            subtask = self.fixture.subtasks[index]
            if index < len(self.fixture.step_records):
                current_evidence = self.fixture.step_records[index]
                self.state.current_record = current_evidence
                if current_evidence not in self.state.records:
                    self.state.records.append(current_evidence)
                    absorb = getattr(self.policy, "absorb", None)
                    if callable(absorb):
                        absorb(self.state, current_evidence)
            else:
                self.state.current_record = ""
            if faults.stale_decision_at == index and self.fixture.stale_values:
                stale = self.fixture.stale_values[0]
                injected = f"旧决策: 常数项 {stale}（来自旧版本记录）"
                if injected not in self.state.records:
                    self.state.records.append(injected)
                    absorb = getattr(self.policy, "absorb", None)
                    if callable(absorb):
                        absorb(self.state, injected)
            context = self.policy.context(self.state, subtask.question)
            # 真实模型会接收截断后的完整 prompt；统计同一字符串，避免把
            # 未发送的长上下文冒充为实际输入成本。
            measured_prompt = context
            build_prompt = getattr(self.researcher, "build_prompt", None)
            if callable(build_prompt):
                measured_prompt = build_prompt(subtask, context)
            tokens = self.policy.count(measured_prompt)
            self.context_tokens += tokens

            if faults.duplicate_submit_from >= 0 and index >= faults.duplicate_submit_from:
                cached = self._idempotent.get(index)
                if cached is not None:
                    self.steps.append(cached)
                    index += 1
                    continue
                step = self._study(subtask, context, tokens, index, faults)
                self._idempotent[index] = step
                self.steps.append(step)
                self._finish_step(subtask, step)
                index += 1
                continue

            step = self._study(subtask, context, tokens, index, faults)
            self.steps.append(step)
            self._finish_step(subtask, step)
            index += 1

        self.cursor = min(index, len(self.fixture.subtasks))
        return self._result()

    def _study(self, subtask: Subtask, context: str, tokens: int,
               index: int, faults: Faults) -> StepResult:
        attempts = 0
        empty_retry_done = False
        while True:
            attempts += 1
            self.model_calls += 1
            if faults.empty_response_at == index and attempts == 1:
                continue  # 模型空响应：按策略重试一次
            if faults.tool_timeout_at == index:
                if attempts < 2:
                    continue  # 第一次超时：按策略重试一次
                return StepResult(
                    subtask_id=subtask.subtask_id, completed=False, context_tokens=tokens,
                    error="timeout", attempts=attempts, model_calls=attempts,
                )
            outcome = self.researcher.answer(subtask, context)
            if not outcome["answer"].strip() and not empty_retry_done:
                empty_retry_done = True
                continue  # 空响应重试一次
            return StepResult(
                subtask_id=subtask.subtask_id,
                completed=bool(outcome["completed"]),
                violation=bool(outcome["violation"]),
                stale_misuse=bool(outcome["stale_misuse"]),
                revised=self.fixture.requires_revision and subtask.forbidden_method != "",
                context_tokens=tokens,
                answer=outcome["answer"],
                error=outcome.get("error", "") or "",
                attempts=attempts,
                model_calls=attempts,
            )

    def _finish_step(self, subtask: Subtask, step: StepResult) -> None:
        if not step.completed:
            return
        # 真实轨迹里工具输出很长：这一截是"全历史"策略的 token 成本来源
        raw_trace = " ".join(f"chunk{i}=候选证据片段" for i in range(24))
        record = (
            f"fact: {subtask.required_fact}\n"
            f"[完成] {subtask.subtask_id}: {subtask.question}\n"
            f"[原始工具输出] {raw_trace}"
        )
        summary = f"[{subtask.subtask_id}] {subtask.question} → {subtask.required_fact}"
        self.policy.observe(self.state, record, summary)

    def _result(self) -> RunResult:
        completed = bool(self.steps) and all(s.completed for s in self.steps)
        return RunResult(
            task_id=self.fixture.task_id,
            family=self.fixture.family,
            policy=self.policy.name,
            completed=completed and self.cursor >= len(self.fixture.subtasks),
            steps=list(self.steps),
            terminal_state="completed" if completed else "failed",
            context_tokens=self.context_tokens,
            model_calls=self.model_calls,
            latency_ms=round(self.context_tokens * 0.5 + self.model_calls * 8.0, 2),
            cost_usd=round(self.context_tokens / 1000.0 * self.price_per_1k, 6),
        )


def run_matrix(fixtures: Sequence[Fixture],
               policies: Dict[str, MemoryPolicy],
               faults: Optional[Faults] = None,
               researcher_factory=None) -> List[RunResult]:
    """三类 fixture × 三种记忆策略，各跑一遍。

    `researcher_factory(fixture)` 可传入 `ModelResearcher` 做真实模型评测；
    不给时是脚本化研究者（离线、确定性）。
    """
    results: List[RunResult] = []
    for fixture in fixtures:
        for name in sorted(policies):
            policy = _fresh_policy(name, policies)
            researcher = (
                researcher_factory(fixture) if researcher_factory is not None else None
            )
            results.append(
                ResearchHarness(fixture, policy, researcher=researcher).run(faults)
            )
    return results


def _fresh_policy(name: str, policies: Dict[str, MemoryPolicy]) -> MemoryPolicy:
    """图状态策略要一个干净的引擎，避免上一条 fixture 的节点串味。"""
    if name == "graph_state":
        return type(policies[name])(
            token_counter=getattr(policies[name], "count", count_words),
        )
    return type(policies[name])(token_counter=getattr(policies[name], "count", count_words))
