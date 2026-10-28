"""阶段 F 的三种记忆策略：同一任务、同一预算，只换"给模型看什么"。

- `full_history`：把全部历史记录原样塞进上下文（基线，token 最高）；
- `narrative_summary`：只给每个子任务的一句话摘要（像普通 RAG/摘要记忆）；
- `graph_state`：规则抽取入图 + 四区组装（约束/决定/事实一直在计划区，原始记录按预算裁剪）。

三者实现同一个接口 `context()`，所以 harness 与指标不需要知道差别。
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from atlas.context.assembler import ContextAssembler
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import NodeType


def count_words(text: str) -> int:
    return max(1, len(str(text or "").split()))


@dataclass
class MemoryState:
    """一次运行里被记忆策略读写的东西。"""

    records: List[str] = field(default_factory=list)
    summaries: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)


class MemoryPolicy:
    name = "base"

    def __init__(self, token_counter: Callable[[str], int] = count_words):
        self.count = token_counter

    def observe(self, state: MemoryState, record: str, summary: str) -> None:
        state.records.append(record)
        state.summaries.append(summary)

    def absorb(self, state: MemoryState, record: str) -> None:
        """把一条记录转成结构化状态；默认策略无可抽取的结构。"""

    def context(self, state: MemoryState, question: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def tokens(self, state: MemoryState, question: str) -> int:
        return self.count(self.context(state, question))


class FullHistoryPolicy(MemoryPolicy):
    name = "full_history"

    def context(self, state: MemoryState, question: str) -> str:
        parts = [f"约束: {c}" for c in state.constraints]
        parts += state.records
        return "\n".join(parts)


class NarrativeSummaryPolicy(MemoryPolicy):
    """只有摘要：短语能过，但约束/精确值容易丢（这正是要测的失败模式）。"""

    name = "narrative_summary"

    def context(self, state: MemoryState, question: str) -> str:
        if not state.summaries:
            return ""
        # 摘要记忆的典型行为：只保留最近两条（更早的压没了）
        return "已完成工作摘要:\n" + "\n".join(state.summaries[-2:])


class GraphStatePolicy(MemoryPolicy):
    """图状态记忆：规则抽取关键行入图，再由四区组装器按预算出上下文。"""

    name = "graph_state"

    def __init__(self, engine: Optional[GraphMemoryEngine] = None,
                 budget_tokens: int = 800,
                 token_counter: Callable[[str], int] = count_words):
        super().__init__(token_counter)
        self.engine = engine or GraphMemoryEngine()
        self.budget_tokens = budget_tokens
        self.assembler = ContextAssembler(
            self.engine, token_counter,
            floors={"summary": 40, "recent": 40, "hints": 20},
        )
        self._seen_facts: set = set()
        self._seen_constraints: set = set()
        self._retired_values: set = set()

    def absorb(self, state: MemoryState, record: str) -> None:
        """规则抽取（不调模型）：`fact:` / `constraint:` 入图；登记被作废的旧值。"""
        import re

        for match in re.finditer(r"旧值\s*(\S+?)\s*已作废", str(record or "")):
            self._retired_values.add(match.group(1))
        for raw_line in str(record or "").splitlines():
            line = raw_line.strip()
            if line.lower().startswith("fact:"):
                content = line[5:].strip()
                if content and content not in self._seen_facts:
                    self.engine.add_fact(content, source="research_tool")
                    self._seen_facts.add(content)
            elif line.lower().startswith("constraint:"):
                content = line[11:].strip()
                if content and content not in self._seen_constraints:
                    self.engine.add_constraint(content, source="user")
                    self._seen_constraints.add(content)

    def observe(self, state: MemoryState, record: str, summary: str) -> None:
        super().observe(state, record, summary)
        for constraint in state.constraints:
            if constraint not in self._seen_constraints:
                self.engine.add_constraint(constraint, source="user")
                self._seen_constraints.add(constraint)
        self.absorb(state, record)

    def context(self, state: MemoryState, question: str) -> str:
        for constraint in state.constraints:  # 用户约束永远先入图（不依赖是否已观测）
            if constraint not in self._seen_constraints:
                self.engine.add_constraint(constraint, source="user")
                self._seen_constraints.add(constraint)
        # 已被作废的旧值不再当摘要喂给模型（状态治理的直接体现）
        summaries = [
            line for line in state.summaries
            if not any(value in line for value in self._retired_values)
        ]
        result = self.assembler.assemble(summaries, [], self.budget_tokens)
        return result.text


def build_policies(token_counter: Callable[[str], int] = count_words,
                   budget_tokens: int = 800) -> Dict[str, MemoryPolicy]:
    return {
        "full_history": FullHistoryPolicy(token_counter),
        "narrative_summary": NarrativeSummaryPolicy(token_counter),
        "graph_state": GraphStatePolicy(budget_tokens=budget_tokens, token_counter=token_counter),
    }


def policy_names(policies: Optional[Dict[str, MemoryPolicy]] = None) -> List[str]:
    return sorted((policies or build_policies()).keys())
