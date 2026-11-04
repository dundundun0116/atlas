"""长程机制的确定性评测：任务生成器 + **独立 oracle** + 故障注入（设计文档 §11 Phase 5）。

为什么要另起一套：原来的 fixture 是**手写**的，判分用的 `probe` 又由写 fixture 的人挑，
于是"出题"和"评分"出自同一只手——评审最容易打的就是这一拳（自建 fixture + probe 判分）。
这里把三件事切成三块，任意一块都不能替另一块说话：

1. **生成器** `generate_tasks`：种子 + 数量 → 题目 / 语料 / gold 全部由种子决定。
   同一个种子必须给出逐字段相同的任务集（有测试钉住），换种子必须不同。
2. **独立 oracle** `AnswerOracle`：只看 gold 与干扰项，**完全不读 agent 自己的 verdict**。
   数值题按整数词边界比对——必须出现 gold，且**不能**出现任何干扰值。
3. **故障注入** `plan_faults` + `FaultCounter`：按种子决定第几题注入哪种故障，
   并把「计划注入」「真的触发了」「压根没派发到工具」分开记。命中率是算出来的。

三档判据彼此独立，报告里分开列（§11 验收要求）：

- **任务成功** = oracle 判的，不是 agent 说 ok；
- **恢复成功** = 卡住之后有没有真的跑到终态，不是 runner 的自述；
- **状态一致** = 结束时的结构性不变量（没有 running 的 Step、没有在途未定论的调用）。

**诚实边界**：这里的检索工具和研究者都是脚本化的，测的是**状态机 / 恢复 / 一致性 / 故障
记账**，不是检索质量，也不是模型能力。所以报告只回答"机制跑通没有"，不回答"效果多好"；
样本量不到门槛时 `mechanism_only=True`，报告里不出现效果数字。
"""

import argparse
import hashlib
import json
import os
import random
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from atlas.api import create_app
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import EvidenceItem, ResearchResult
from atlas.tools.protocol import ResearchToolError

#: 生成器用的词表（都是固定常量，改它等于改题集 ⇒ 指纹会变，报告里看得见）。
#:
#: **为什么全是虚构型号**：这组题用来测"证据有没有被送到模型面前、状态与恢复对不对"，
#: 不是测世界知识。如果用真实实体（"爱因斯坦的出生年份"）配随机 gold，真模型会按
#: 世界知识答 1879 而不是我们给的 1914——测出来的就成了"模型肯不肯抄假事实"。
#: 换成现实里不存在的型号后，答案**只可能**来自检索到的资料。
SUBJECTS = (
    "K-17 型传感器", "MX-204 型继电器", "ZR-9 型光谱仪", "TA-33 型陀螺仪",
    "PQ-8 型电容器", "VH-51 型变频器", "LS-2 型激光器", "AB-77 型编码器",
    "CN-15 型调制器", "DG-40 型稳压源", "FR-6 型滤波器", "JW-19 型示波器",
)
ATTRIBUTES = ("标定年份", "首次出厂年份", "改版年份")

#: 效果数字的门槛：少于这么多题就只报机制验证（§11 验收）。
MIN_N_FOR_EFFECT = 30


# ---------------------------------------------------------------- 任务与判分


@dataclass(frozen=True)
class Chunk:
    source: str
    chunk_id: str
    quote: str

    def to_dict(self) -> Dict[str, str]:
        return {"source": self.source, "chunk_id": self.chunk_id, "quote": self.quote}


@dataclass(frozen=True)
class GeneratedTask:
    """一道由种子生成的题：问题 + gold + 语料（含干扰项）。"""

    task_id: str
    question: str
    gold: str
    chunks: Tuple[Chunk, ...]
    distractors: Tuple[str, ...]

    @property
    def gold_chunk(self) -> Chunk:
        return next(item for item in self.chunks if f"ANSWER:{self.gold}" in item.quote)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "gold": self.gold,
            "distractors": list(self.distractors),
            "chunks": [item.to_dict() for item in self.chunks],
        }


def generate_tasks(count: int = 8, seed: int = 7) -> List[GeneratedTask]:
    """按种子生成固定题集；同一 `(count, seed)` 必须给出逐字段相同的结果。"""
    if count <= 0:
        raise ValueError("count 必须为正")
    rng = random.Random(seed)
    # 年份池不重复采样：避免两道题的 gold 撞车，否则"答对"会互相污染。
    # 区间取现代年份，免得"1953 年标定的传感器"读起来太离谱。
    years = list(range(1950, 2085, 1))
    rng.shuffle(years)
    if 2 * count + count > len(years):  # gold + 每题两个干扰项
        raise ValueError(f"年份池不够：count={count}")
    pool_index = 0

    tasks: List[GeneratedTask] = []
    for index in range(count):
        subject = SUBJECTS[index % len(SUBJECTS)]
        attribute = ATTRIBUTES[(index // len(SUBJECTS)) % len(ATTRIBUTES)]
        gold = str(years[pool_index])
        distractors = tuple(str(years[pool_index + offset]) for offset in (1, 2))
        pool_index += 3

        chunks = [
            Chunk(source=f"内部技术档案 《{subject} 卷》第 3 版",
                  chunk_id=f"c{index:02d}-gold",
                  quote=f"档案原文：{subject}的{attribute}为 ANSWER:{gold}。"),
        ]
        for order, value in enumerate(distractors):
            chunks.append(Chunk(
                source=f"二手资料 {order + 1}", chunk_id=f"c{index:02d}-d{order}",
                quote=f"另一份未署名的二手备忘录提到 {value} 年，但未标注出处，可信度存疑。",
            ))
        rng.shuffle(chunks)
        tasks.append(GeneratedTask(
            task_id=f"T{index + 1:02d}",
            question=f"{subject}的{attribute}是哪一年？",
            gold=gold,
            chunks=tuple(chunks),
            distractors=distractors,
        ))
    return tasks


def _normalize(text: str) -> str:
    """归一化只做"读法差异"：全半角、大小写、空白。**不做**语义近似。"""
    return unicodedata.normalize("NFKC", str(text or "")).strip()


@dataclass(frozen=True)
class OracleVerdict:
    passed: bool
    reason: str = ""
    numbers: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason,
                "numbers": list(self.numbers)}


class AnswerOracle:
    """**独立**判分：只比对 gold 与干扰项，不读 agent 的任何自我评价。

    这条独立性是整套评测的地基：如果判分去信 agent 的 `verdict`，
    那 agent 说 ok 就等于成功，测出来的只是"它自信不自信"。
    """

    def grade(self, answer: str, task: GeneratedTask) -> OracleVerdict:
        text = _normalize(answer)
        if not text:
            return OracleVerdict(False, "答案为空")
        numbers = tuple(re.findall(r"\d+", text))
        if task.gold not in numbers:
            return OracleVerdict(False, f"答案里没有 gold={task.gold}", numbers)
        wrong = [value for value in task.distractors if value in numbers]
        if wrong:
            return OracleVerdict(False, f"答案里出现了干扰值 {wrong}", numbers)
        return OracleVerdict(True, "", numbers)


# ---------------------------------------------------------------- 故障注入


@dataclass(frozen=True)
class FaultPlan:
    """第几题注入哪种故障；由种子决定，可复现。"""

    assignments: Dict[str, str] = field(default_factory=dict)

    def for_task(self, task_id: str) -> Optional[str]:
        return self.assignments.get(task_id)

    @property
    def count(self) -> int:
        return len(self.assignments)

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "assignments": dict(self.assignments)}


def plan_faults(tasks: Sequence[GeneratedTask], *, every: int = 3,
                kinds: Sequence[str] = ("timeout", "invalid_request")) -> FaultPlan:
    """每 `every` 题注入一次，种类轮转；`every<=0` 表示不注入。"""
    if every <= 0:
        return FaultPlan({})
    if not kinds:
        raise ValueError("kinds 不能为空")
    assignments: Dict[str, str] = {}
    slot = 0
    for index, task in enumerate(tasks):
        if (index + 1) % every != 0:
            continue
        assignments[task.task_id] = kinds[slot % len(kinds)]
        slot += 1
    return FaultPlan(assignments)


def expectation_for(kind: str) -> str:
    """这道题**应该**得到什么结果——由故障语义决定，不看出题人心情。

    - 无故障 / `timeout`：工具最终能给出证据 ⇒ 应该答对（timeout 要先对账再续跑）；
    - 其它确定性失败（如 `invalid_request`）：一步拿不到证据 ⇒ 应该**确定性失败**，
      而且不能重试风暴。
    """
    return "failed" if kind and kind not in ("timeout",) else "passed"


@dataclass
class FaultCounter:
    """把「计划注入」「真的触发」「压根没派发到工具」分开记。

    只报一个"注入率"是不够的：如果某道题的计划压根没跑起来，
    那次注入根本没发生，把它算成"注入了但恢复失败"就是在冤枉系统。
    """

    planned: Dict[str, str] = field(default_factory=dict)
    hit: Dict[str, str] = field(default_factory=dict)

    def fired(self, task_id: str) -> bool:
        return task_id in self.hit

    def record(self, task_id: str, kind: str) -> None:
        self.hit[task_id] = kind

    @property
    def missed(self) -> List[str]:
        return [task_id for task_id in self.planned if task_id not in self.hit]

    @property
    def hit_rate(self) -> Optional[float]:
        if not self.planned:
            return None
        return len(self.hit) / len(self.planned)

    def to_dict(self) -> Dict[str, Any]:
        return {"planned": dict(self.planned), "hit": dict(self.hit),
                "missed": self.missed, "hit_rate": self.hit_rate}


# ---------------------------------------------------------------- 脚本化的“研究者”

_ANSWER = re.compile(r"ANSWER:(\d+)")


#: 发给真模型的统一前置说明。**这一点必须如实写进报告**：
#: 本评测测的是运行时机制，不测世界知识，所以明确告诉模型"资料虚构、只用资料答题"。
#: 不这么做，模型会拿世界知识纠正我们给的 gold，测出来的就不是机制了。
EVAL_PREAMBLE = (
    "【评测说明】下面是虚构档案里的一道题，用于测试运行时行为：\n"
    "1) 只根据提示中出现过的资料作答，不要用世界知识纠正或补充；\n"
    "2) 资料中形如 `ANSWER:<数字>` 的标记就是本题的权威答案；\n"
    "3) 资料里没有答案时，直接说明「上下文里没有可用证据」，不要猜；\n"
    "4) **最终答案只写你要给的那个结论**：不要在答案里复述、对比或讨论其它候选数字\n"
    "   （判分是确定性的：答案正文里出现别的数字就算错，哪怕你写的是“不采用”）；\n"
    "5) 要求输出 JSON 时只输出 JSON。\n\n"
)


class ApiReader:
    """真模型研究者：OpenAI 兼容端点，逐次记账真实 usage。

    与 `ScriptedReader` 的区别只在一处——它**不是**确定性的。所以报告里要把它与
    脚本化结果分开标：`reader_kind="api"`，token 数来自响应的 `usage`（`measured`），
    而不是按字数估的 `synthetic_estimate`。
    """

    kind = "api"

    def __init__(self, model: str, *, base_url: str = "", api_key: str = "",
                 temperature: float = 0.0, timeout_s: float = 180.0,
                 session: str = "atlas-longhorizon", preamble: str = EVAL_PREAMBLE,
                 transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                 max_attempts: int = 3, backoff_s: float = 2.0) -> None:
        if not model:
            raise ValueError("ApiReader 需要 model")
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.session = session
        self.preamble = preamble
        self.transport = transport or self._post
        self.max_attempts = max(1, max_attempts)
        self.backoff_s = max(0.0, backoff_s)
        #: 每次调用的记账（token 全是响应里拿的，不是估的）
        self.calls: List[Dict[str, Any]] = []

    # ---------- 记账 ----------

    @property
    def totals(self) -> Dict[str, Any]:
        prompt = sum(item["prompt_tokens"] for item in self.calls)
        completion = sum(item["completion_tokens"] for item in self.calls)
        latency = sum(item["latency_s"] for item in self.calls)
        cost = sum(item.get("cost_usd") or 0.0 for item in self.calls)
        return {"calls": len(self.calls), "prompt_tokens": prompt,
                "completion_tokens": completion, "latency_s": latency,
                "cost_usd": cost, "source": "measured"}

    # ---------- 调用 ----------

    def __call__(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": self.preamble + prompt}],
            "temperature": self.temperature,
        }
        last: Optional[BaseException] = None
        for attempt in range(1, self.max_attempts + 1):
            started = time.time()
            try:
                response = self.transport(payload)
            except urllib.error.HTTPError as exc:
                # HTTPError 是 URLError 的子类，**必须先判**：4xx 是调用方的错，重试没意义
                last = exc
                if exc.code in (408, 425, 429) or exc.code >= 500:
                    time.sleep(min(self.backoff_s * attempt, 6.0))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
                time.sleep(min(self.backoff_s * attempt, 6.0))
                continue
            latency = time.time() - started
            usage = response.get("usage") or {}
            self.calls.append({
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cost_usd": float((usage.get("opencode") or {}).get("list_cost") or 0.0),
                "latency_s": latency,
                "attempt": attempt,
            })
            choices = response.get("choices") or [{}]
            return str((choices[0].get("message") or {}).get("content") or "")
        raise RuntimeError(f"模型调用连续失败 {self.max_attempts} 次：{last}")

    # ---------- 传输 ----------

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url or not self.api_key:
            raise RuntimeError(
                "真模型需要 OPENAI_BASE_URL 与 OPENAI_API_KEY（可 source eval/.env）")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": os.environ.get("ATLAS_EVAL_UA", "atlas-longhorizon/1.0"),
        }
        # opencode zen 端点不带这个头会 400 MissingSessionID
        if os.environ.get("OPENCODE_COMPAT", "1") == "1":
            headers["x-opencode-session"] = os.environ.get(
                "OPENCODE_SESSION", self.session)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))


class ScriptedReader:
    """脚本化研究者：上下文里有 `ANSWER:<n>` 就答它，没有就明说不知道。

    它**故意**只会抄：这样"答对/答错"只取决于运行时有没把证据送到模型面前，
    故障、恢复、上下文组装的差异才能被单独观测到（真实模型的随机性会盖掉这些差异）。
    """

    kind = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if "AUDITOR" in prompt or "auditing" in prompt.lower():
            return json.dumps({"verdict": "ok", "issues": "", "refs": []})
        found = _ANSWER.findall(prompt)
        if found:
            return f"答案是 {found[0]} 年。"
        return "上下文里没有可用证据，无法回答。"


class SuiteResearchTool:
    """把生成出来的语料喂给检索步；按故障计划在**产出结果之后**抛错。

    "产出之后才抛"是有意的：真实超时最常见的现场是服务端算完了、响应没回来。
    这样对账时能真的查回原结果，`reconcile → 重开 → 恢复` 这条路才是真的走通了。
    """

    def __init__(self, tasks: Sequence[GeneratedTask], faults: FaultPlan,
                 counter: FaultCounter) -> None:
        self.tasks = tuple(tasks)
        self.faults = faults
        self.counter = counter
        self._served: Dict[str, Dict[str, Any]] = {}
        self.calls = 0

    def research(self, request) -> ResearchResult:
        # 问题文本外面会裹一层计划标题（"研究流程（plan_dag_v1）：…"），按包含匹配
        task = next((item for item in self.tasks if item.question in str(request.query)), None)
        if task is None:
            raise ResearchToolError("invalid_request", f"未知题目：{request.query}")
        self.calls += 1
        result = self._build(task)
        self._served[task.task_id] = _result_payload(result)
        kind = self.faults.for_task(task.task_id)
        if kind and not self.counter.fired(task.task_id):
            self.counter.record(task.task_id, kind)
            raise ResearchToolError(kind, f"注入故障：{kind}（结果已产出，响应丢失）")
        return result

    def served_output(self, task_id: str) -> Dict[str, Any]:
        """对账时要查回的那份结果（模拟"服务端其实算完了"）。"""
        return dict(self._served.get(task_id) or {})

    @staticmethod
    def _build(task: GeneratedTask) -> ResearchResult:
        return ResearchResult(
            answer=f"{task.gold} 年",
            evidence=[EvidenceItem(source=chunk.source, chunk_id=chunk.chunk_id,
                                   quote=chunk.quote, score=0.9, retriever="eval",
                                   rank=index + 1)
                      for index, chunk in enumerate(task.chunks)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id=f"eval-{task.task_id}",
        )


def _result_payload(result: ResearchResult) -> Dict[str, Any]:
    return {
        "refs": [item.to_dict() for item in result.evidence],
        "answer": result.answer,
        "stop_reason": result.stop_reason,
        "research_trace_id": result.trace_id,
    }


# ---------------------------------------------------------------- 单题执行


@dataclass
class TaskOutcome:
    task_id: str
    question: str
    gold: str
    fault: str = ""
    #: 运行到终态（completed / failed）
    state: str = ""
    #: oracle 说的，不是 agent 说的
    passed: bool = False
    answer: str = ""
    reason: str = ""
    #: 卡住之后有没有真的续跑到终态
    needed_resume: bool = False
    resumed: bool = False
    reconciles: int = 0
    reopens: int = 0
    context_digests: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    #: 这道题**应该**出现的结果（由故障种类决定，与 oracle 无关）
    expected: str = "passed"
    #: 工具被真正调用了几次（"未知不重发"要靠这个数来证，不能靠嘴说）
    tool_invocations: int = 0
    #: 真模型跑出来的用量（脚本化研究者时恒为 0/None）
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = None

    @property
    def consistent(self) -> bool:
        return not self.issues

    @property
    def as_expected(self) -> bool:
        """行为是否符合预期——比"答对率"更能说清故障下的表现。

        注入了确定性失败的题**本来就该失败**，把它算进"答对率"是在自己为难自己；
        但"该失败的直接失败了"不等于"跑对了"，还要看有没有重试风暴、状态是否一致。
        """
        if self.expected == "failed":
            return self.state == "failed" and self.consistent
        return self.passed and self.consistent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id, "question": self.question, "gold": self.gold,
            "fault": self.fault, "state": self.state, "passed": self.passed,
            "answer": self.answer, "reason": self.reason,
            "needed_resume": self.needed_resume, "resumed": self.resumed,
            "reconciles": self.reconciles, "reopens": self.reopens,
            "context_digests": list(self.context_digests),
            "issues": list(self.issues),
            "expected": self.expected,
            "as_expected": self.as_expected,
            "tool_invocations": self.tool_invocations,
            "model_calls": self.model_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
        }


#: 结束时不该出现的 Step 状态（还在飞）
_IN_FLIGHT_STEP = frozenset({"running", "verifying", "dispatched", "ready"})
#: 结束时不该出现的工具调用状态（还没定论、又没人打算定论）
_UNRESOLVED_CALL = frozenset({"dispatched", "indeterminate", "reconciling",
                              "compensating"})


def _latest_draft(repository, session_id: str, plan_id: str) -> str:
    drafts = [item for item in repository.attempts(session_id, plan_id, "draft")
              if item["status"] == "succeeded"]
    if not drafts:
        return ""
    return str((drafts[-1]["output"] or {}).get("draft") or "")


def _consistency(registry: GraphRepository, session_id: str, plan_id: str,
                 state: str) -> List[str]:
    """结束时的结构性不变量；与"答对没答对"无关。"""
    issues: List[str] = []
    if state not in ("completed", "failed"):
        issues.append(f"任务没有到终态（{state}）")
    statuses = registry.step_statuses(session_id, plan_id)
    for step_id, status in sorted(statuses.items()):
        if status in _IN_FLIGHT_STEP:
            issues.append(f"Step {step_id} 停在 {status}")
    for call in registry.tool_calls(session_id):
        if call["status"] in _UNRESOLVED_CALL:
            issues.append(f"工具调用 {call['tool_call_id']} 停在 {call['status']}")
    return issues


def _resume(client, registry: GraphRepository, session_id: str, plan_id: str,
            tool: SuiteResearchTool, task: GeneratedTask,
            outcome: TaskOutcome) -> None:
    """扮演值班的人：先把在途未知调用对账，再显式重开，然后恢复。"""
    calls = client.get(f"/tasks/{session_id}/tool-calls").json()["tool_calls"]
    for call in calls:
        if call["status"] not in ("indeterminate", "dispatched"):
            continue
        client.post(
            f"/tasks/{session_id}/tool-calls/{call['tool_call_id']}/reconcile",
            json={"outcome": "applied", "detail": "按幂等键查回原结果",
                  "result": tool.served_output(task.task_id)},
        )
        outcome.reconciles += 1

    detail = client.get(f"/tasks/{session_id}/plans/{plan_id}").json()
    stuck = [step["step_id"] for step in detail["steps"]
             if step["status"] == "needs_human"]
    client.post(f"/tasks/{session_id}/plans/{plan_id}/recover",
                json={"dry_run": False, "reopen_steps": stuck})
    outcome.reopens += len(stuck)


def _reader_usage(reader) -> Dict[str, Any]:
    totals = getattr(reader, "totals", None)
    return dict(totals) if isinstance(totals, dict) else {}


def run_task(client, registry, tool, task: GeneratedTask,
             outcome: TaskOutcome, *, session_id: str, plan_id: str = "P1",
             reader: Any = None) -> None:
    question = task.question
    state_before = tool.calls
    usage_before = _reader_usage(reader)
    client.post("/tasks", json={"session_id": session_id, "goal": question})
    client.post(f"/tasks/{session_id}/plans",
                json={"problem": question, "activate": True})
    client.post(f"/tasks/{session_id}/plans/{plan_id}/advance", json={})

    state = client.get(f"/tasks/{session_id}").json()["state"]
    if state == "paused":
        outcome.needed_resume = True
        _resume(client, registry, session_id, plan_id, tool, task, outcome)
        state = client.get(f"/tasks/{session_id}").json()["state"]
        outcome.resumed = state in ("completed", "failed")

    outcome.tool_invocations = tool.calls - state_before
    if reader is not None:
        after = _reader_usage(reader)
        outcome.model_calls = int(after.get("calls", 0)) - int(usage_before.get("calls", 0))
        outcome.prompt_tokens = (int(after.get("prompt_tokens", 0))
                                 - int(usage_before.get("prompt_tokens", 0)))
        outcome.completion_tokens = (int(after.get("completion_tokens", 0))
                                     - int(usage_before.get("completion_tokens", 0)))
        outcome.cost_usd = (float(after.get("cost_usd", 0.0))
                            - float(usage_before.get("cost_usd", 0.0)))
    outcome.state = state
    outcome.answer = _latest_draft(registry, session_id, plan_id)
    verdict = AnswerOracle().grade(outcome.answer, task)
    outcome.passed = verdict.passed
    outcome.reason = verdict.reason
    outcome.issues = _consistency(registry, session_id, plan_id, state)
    if outcome.tool_invocations > 1:
        # 未知结果被重发过——这是这套协议最不能出的事，直接记成违例
        outcome.issues.append(f"工具被调用了 {outcome.tool_invocations} 次（未知结果不得重发）")
    packages = client.get(f"/tasks/{session_id}/contexts").json()["packages"]
    outcome.context_digests = [item["digest"] for item in packages if item["digest"]]


# ---------------------------------------------------------------- 汇总


def _wilson_lower(passed: int, total: int, z: float = 1.96) -> Optional[float]:
    if total <= 0:
        return None
    phat = passed / total
    denominator = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * ((phat * (1 - phat) / total + z * z / (4 * total * total)) ** 0.5)
    return max(0.0, (centre - margin) / denominator)


@dataclass
class SuiteConfig:
    count: int = 8
    seed: int = 7
    fault_every: int = 3
    fault_kinds: Tuple[str, ...] = ("timeout", "invalid_request")
    min_n_for_effect: int = MIN_N_FOR_EFFECT
    #: 空 = 脚本化研究者（确定性、零成本）；非空 = 真模型（走 OpenAI 兼容端点）
    model: str = ""
    temperature: float = 0.0
    timeout_s: float = 180.0

    @property
    def reader_kind(self) -> str:
        return "api" if self.model else "scripted"

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "seed": self.seed,
                "fault_every": self.fault_every,
                "fault_kinds": list(self.fault_kinds),
                "min_n_for_effect": self.min_n_for_effect,
                "model": self.model, "temperature": self.temperature,
                "timeout_s": self.timeout_s}

    def fingerprint(self) -> str:
        """配置指纹：同配置同指纹，改一个字段就变——报告里用来钉住"跑的是哪一套"。"""
        blob = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class SuiteReport:
    config: SuiteConfig
    tasks: List[TaskOutcome]
    faults: FaultCounter
    question_digest: str = ""
    reader_kind: str = "scripted"
    usage: Dict[str, Any] = field(default_factory=dict)

    # ---------- 三档独立判据 ----------

    @property
    def n(self) -> int:
        return len(self.tasks)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.tasks if item.passed)

    @property
    def success_rate(self) -> Optional[float]:
        return self.passed / self.n if self.n else None

    @property
    def wilson_lower(self) -> Optional[float]:
        return _wilson_lower(self.passed, self.n)

    @property
    def mechanism_only(self) -> bool:
        """只报机制验证、不报效果数字（§11 验收）。两个条件任一成立：

        - 样本量不到门槛；
        - 研究者是**脚本化**的——它证明的是"机制在故障下不自毁"，不是模型能力。
        """
        return self.n < self.config.min_n_for_effect or self.reader_kind != "api"

    @property
    def resume_required(self) -> List[str]:
        return [item.task_id for item in self.tasks if item.needed_resume]

    @property
    def resume_succeeded(self) -> List[str]:
        return [item.task_id for item in self.tasks
                if item.needed_resume and item.resumed]

    @property
    def recovery_rate(self) -> Optional[float]:
        required = self.resume_required
        return len(self.resume_succeeded) / len(required) if required else None

    @property
    def inconsistent(self) -> List[TaskOutcome]:
        return [item for item in self.tasks if not item.consistent]

    # ---------- 分队列：干净题看能力，注入题看行为 ----------

    @property
    def clean(self) -> List[TaskOutcome]:
        return [item for item in self.tasks if not item.fault]

    @property
    def faulted(self) -> List[TaskOutcome]:
        return [item for item in self.tasks if item.fault]

    @property
    def clean_rate(self) -> Optional[float]:
        return (sum(1 for item in self.clean if item.passed) / len(self.clean)
                if self.clean else None)

    @property
    def clean_wilson_lower(self) -> Optional[float]:
        return _wilson_lower(sum(1 for item in self.clean if item.passed),
                             len(self.clean))

    @property
    def as_expected(self) -> List[str]:
        return [item.task_id for item in self.tasks if item.as_expected]

    @property
    def behaved_rate(self) -> Optional[float]:
        return len(self.as_expected) / self.n if self.n else None

    @property
    def digest(self) -> str:
        rows = json.dumps([item.to_dict() for item in self.tasks],
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(rows.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "config_fingerprint": self.config.fingerprint(),
            "reader_kind": self.reader_kind,
            "usage": dict(self.usage),
            "question_digest": self.question_digest,
            "run_digest": self.digest,
            "mechanism_only": self.mechanism_only,
            "n": self.n,
            "passed": self.passed,
            "success_rate": self.success_rate,
            "wilson_lower": self.wilson_lower,
            "recovery": {"required": self.resume_required,
                         "succeeded": self.resume_succeeded,
                         "rate": self.recovery_rate},
            "faults": self.faults.to_dict(),
            "consistency": {"checked": self.n,
                            "violations": {item.task_id: item.issues
                                           for item in self.inconsistent}},
            "cohorts": {
                "clean": {"n": len(self.clean),
                          "passed": sum(1 for item in self.clean if item.passed),
                          "rate": self.clean_rate},
                "faulted": {"n": len(self.faulted),
                            "as_expected": len([item for item in self.faulted
                                                if item.as_expected])},
                "as_expected": self.as_expected,
                "behaved_rate": self.behaved_rate,
            },
            "tasks": [item.to_dict() for item in self.tasks],
        }

    def to_markdown(self) -> str:
        lines = ["# 长程评测报告（确定性）", ""]
        lines.append(f"- 配置指纹：`{self.config.fingerprint()}`"
                     f"（count={self.config.count} seed={self.config.seed} "
                     f"fault_every={self.config.fault_every}）")
        lines.append(f"- 题集指纹：`{self.question_digest}`　运行指纹：`{self.digest}`")
        lines.append(f"- 研究者：**{self.reader_kind}**"
                     + (f"（{self.config.model}）" if self.config.model else "（脚本化，非模型）"))
        if self.mechanism_only:
            lines.append(f"- 样本量 {self.n} < {self.config.min_n_for_effect}"
                         " ⇒ **只报机制验证，不报效果数字**")
        lines += ["", "## 三档独立判据", ""]
        clean_rate = "—" if self.clean_rate is None else f"{self.clean_rate:.3f}"
        clean_ci = ("" if self.mechanism_only or self.clean_wilson_lower is None
                    else f"（Wilson 下界 {self.clean_wilson_lower:.3f}）")
        lines.append(f"- **干净题答对**（oracle，{len(self.clean)} 题）："
                     f"{clean_rate}{clean_ci}")
        lines.append(f"- **注入题行为符合预期**："
                     f"{len([i for i in self.faulted if i.as_expected])}/{len(self.faulted)}")
        # 含注入题的总数**不是**能力数字：注入了确定性失败的题本来就不该通过
        lines.append(f"- （参考）全部题 oracle 通过：{self.passed}/{self.n}"
                     "——含注入题，别当成能力数字")
        lines.append(f"- 没有盲发：工具调用次数 {sum(i.tool_invocations for i in self.tasks)}"
                     f"（{self.n} 题，每题最多 1 次）")
        recovery = "—" if self.recovery_rate is None else f"{self.recovery_rate:.3f}"
        lines.append(f"- 恢复成功：{len(self.resume_succeeded)}/"
                     f"{len(self.resume_required)}（{recovery}）")
        lines.append(f"- 状态一致：{self.n - len(self.inconsistent)}/{self.n} 通过")
        if self.usage:
            lines.append(
                f"- 用量（measured）：{self.usage.get('calls', 0)} 次模型调用，"
                f"prompt {self.usage.get('prompt_tokens', 0)} tok，"
                f"completion {self.usage.get('completion_tokens', 0)} tok，"
                f"累计 {self.usage.get('latency_s', 0.0):.1f}s，"
                f"${self.usage.get('cost_usd', 0.0):.4f}")
        faults = self.faults
        hit_rate = "—" if faults.hit_rate is None else f"{faults.hit_rate:.3f}"
        lines.append(f"- 故障注入：计划 {len(faults.planned)} 次，实际命中 "
                     f"{len(faults.hit)} 次（{hit_rate}），"
                     f"没派发到工具 {len(faults.missed)} 次")
        lines += ["", "## 逐题", "",
                  "| 题 | 故障 | 终态 | 预期 | oracle | 恢复 | 调用 | 一致 | 答案 |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for item in self.tasks:
            lines.append(
                f"| {item.task_id} | {item.fault or '—'} | {item.state} | "
                f"{item.expected} | {'过' if item.passed else '不过'} | "
                f"{'是' if item.needed_resume else '—'}"
                f"{'/已续跑' if item.resumed else ''} | "
                f"{item.tool_invocations} | "
                f"{'✓' if item.consistent else '✗'} | {item.answer[:24]} |")
        return "\n".join(lines)


def _token_counter() -> Callable[[str], int]:
    """上下文预算用的计数器：优先真 tokenizer（tiktoken），退化成按字数估。"""
    try:
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(encoding.encode(str(text or "")))
    except Exception:  # pragma: no cover - 没装 tiktoken 时命中
        return lambda text: (len(str(text or "")) + 3) // 4


def run_suite(config: Optional[SuiteConfig] = None, *,
              database_path: str = "") -> SuiteReport:
    """把整套题跑一遍（脚本化研究者 + 脚本化检索工具，不联网、不烧 token）。"""
    cfg = config or SuiteConfig()
    tasks = generate_tasks(count=cfg.count, seed=cfg.seed)
    faults = plan_faults(tasks, every=cfg.fault_every, kinds=cfg.fault_kinds)
    counter = FaultCounter(planned=dict(faults.assignments))
    tool = SuiteResearchTool(tasks, faults, counter)
    if cfg.model:
        reader = ApiReader(cfg.model, temperature=cfg.temperature,
                           timeout_s=cfg.timeout_s)
    else:
        reader = ScriptedReader()

    question_digest = hashlib.sha256(
        json.dumps([task.to_dict() for task in tasks], ensure_ascii=False,
                   sort_keys=True).encode("utf-8")).hexdigest()[:16]

    outcomes: List[TaskOutcome] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = database_path or os.path.join(tmp, "suite.db")
        database = open_database(path)
        registry = GraphRepository(database)
        try:
            app = create_app(
                registry, research_tool=tool, orchestrate_llm=reader,
                context_token_counter=_token_counter(),
            )
            from fastapi.testclient import TestClient

            client = TestClient(app)
            for index, task in enumerate(tasks):
                kind = faults.for_task(task.task_id) or ""
                outcome = TaskOutcome(task_id=task.task_id,
                                      question=task.question, gold=task.gold,
                                      fault=kind, expected=expectation_for(kind))
                run_task(client, registry, tool, task, outcome,
                         session_id=f"eval-{index:02d}", reader=reader)
                outcomes.append(outcome)
        finally:
            database.close()

    usage = dict(getattr(reader, "totals", {}) or {})
    return SuiteReport(config=cfg, tasks=outcomes, faults=counter,
                       question_digest=question_digest,
                       reader_kind=getattr(reader, "kind", "scripted"),
                       usage=usage)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="长程机制的确定性评测（生成器 + 独立 oracle + 故障注入）")
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fault-every", type=int, default=3)
    parser.add_argument("--model", default="",
                        help="真模型名（留空 = 脚本化研究者）；需要 OPENAI_BASE_URL/KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--md", dest="md_path", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run_suite(SuiteConfig(count=args.tasks, seed=args.seed,
                                   fault_every=args.fault_every,
                                   model=args.model,
                                   temperature=args.temperature,
                                   timeout_s=args.timeout))
    text = report.to_markdown()
    print(text)
    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)), exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写入 {args.json_path}")
    if args.md_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.md_path)), exist_ok=True)
        with open(args.md_path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"Markdown 已写入 {args.md_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
