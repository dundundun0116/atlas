"""结转触发器与管理器（决策清单 #2：子目标边界优先结转 + 80% 阈值强制兜底）。

结转顺序（结转式压缩的核心，先于摘要）：
  1. extractor 把原始记录中的状态结构化写入图（真实实现为 LLM 提取，测试可注入替身）
  2. 生成带全局步号的叙事摘要段（摘要只管叙事，不承载状态——状态在图里）
  3. 标注本次边界完成的子任务（只报一次）
调用方随后丢弃 recent_raw 缓冲。
"""

from typing import Callable, List, Optional, Sequence

from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import NodeStatus, NodeType


class CarryOverTrigger:
    """子目标边界（自上次结转后有新完成的子任务）或用量 >= threshold 时触发。"""

    def __init__(
        self,
        engine: GraphMemoryEngine,
        token_counter: Callable[[str], int],
        budget_tokens: int,
        threshold: float = 0.8,
    ):
        self.engine = engine
        self.count = token_counter
        self.budget_tokens = budget_tokens
        self.threshold = threshold
        self._seen_done = self._done_ids()

    def should_carry(self, recent_raw: Sequence[str]) -> bool:
        if self._done_ids() - self._seen_done:
            return True
        usage = self.count("\n".join(recent_raw)) if recent_raw else 0
        return usage >= self.threshold * self.budget_tokens

    def mark_carried(self) -> None:
        self._seen_done = self._done_ids()

    def _done_ids(self) -> set:
        return {
            n.id for n in self.engine.nodes.values()
            if n.type == NodeType.SUBTASK and n.status == NodeStatus.DONE
        }


class CarryOverManager:
    def __init__(self, engine: GraphMemoryEngine, token_counter: Callable[[str], int]):
        self.engine = engine
        self.count = token_counter
        self.step = 0
        self._seen_done: set = set()

    def execute(
        self,
        recent_raw: Sequence[str],
        extractor: Optional[Callable[[Sequence[str], GraphMemoryEngine], None]] = None,
    ) -> List[str]:
        if not recent_raw:
            return []
        # 1) 状态先入图（提取器可返回 ExtractionResult：其 summary 作为叙事摘要）
        result = None
        if extractor is not None:
            result = extractor(recent_raw, self.engine)
        # 2) 带全局步号的叙事摘要（LLM 摘要优先，缺失时退化为逐行占位）
        self.step += len(recent_raw)
        start = self.step - len(recent_raw)
        llm_summary = getattr(result, "summary", "") if result is not None else ""
        if llm_summary:
            summary = [f"[步骤 {start + 1}–{self.step}] {llm_summary}"]
        else:
            summary = [
                f"[步骤 {start + i + 1}] {line}" for i, line in enumerate(recent_raw)
            ]
        # 3) 边界标注（只报一次）
        newly_done = [
            n for n in self.engine.nodes.values()
            if n.type == NodeType.SUBTASK
            and n.status == NodeStatus.DONE
            and n.id not in self._seen_done
        ]
        for n in newly_done:
            self._seen_done.add(n.id)
        if newly_done:
            summary.append(
                "（子目标边界："
                + "、".join(f"{n.id} {n.content} 完成" for n in newly_done)
                + "）"
            )
        return summary
