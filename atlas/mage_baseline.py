"""MAGE 简化基线（MAGELiteMemorySystem）：叙事摘要记忆，Atlas 的中间对照臂。

复现边界（精读笔记 #8）：无树、无 Maintain、无 Revise——Update = 子任务边界用 LLM
把上一子任务原始记录压缩成一条叙事摘要（C 区累计）、清近期区；Retrieve = 返回 C+R，
在 math 域每子任务 1 add 的节奏下 R 恒空，即纯摘要基线。与 Atlas 臂同边界结转、同
LLM，唯一差异是记忆形态（叙事摘要 vs 结构化图）——这正是对照要测的假设。

无预算配比（MAGE 本性：C 随子任务数自然增长，精读笔记 #4）。摘要失败（空正文/异常）
降级为截断原文，保证跑分不中断；carry_log/extract_errors 与 Atlas 同名，服务端
_flush_events 零改动复用。
"""

from typing import Callable, List, Optional

_SUMMARY_INSTRUCTION = (
    "以下是一个求解数学问题的 agent 在一个子任务中的执行记录。"
    "把它压缩成一段简洁的摘要（150 字以内），保留后续子任务可能需要的"
    "关键结论、定义与数值。只输出摘要正文。\n\n"
)
_FALLBACK_CHARS = 500


class MAGELiteMemorySystem:
    def __init__(
        self,
        user_id: str,
        llm: Callable[[str], str],
        token_counter: Callable[[str], int],
    ):
        self.user_id = user_id
        self.llm = llm
        self.count = token_counter  # 接口与 Atlas 一致；MAGE 无预算概念，暂不参与裁剪
        self.buffer: List[str] = []
        self.summaries: List[str] = []
        self.pending = False  # 是否已有已开始的子任务（首个 wrap 前的噪声记录据此丢弃）
        self.carry_log: List[str] = []
        self.extract_errors: List[str] = []
        self.fallbacks = 0

    # ---------- 官方三端点对应的行为 ----------

    def add_chunk(self, chunk: str) -> None:
        self.buffer.append(chunk)

    def wrap_user_prompt(self, question: str) -> str:
        if self.buffer and self.pending:
            self._summarize_buffer()
        elif self.buffer:
            # 首个 wrap 之前的记录（runner 会先 add "Initial result: Empty"）：直接丢弃
            self.buffer = []
        self.pending = True
        return f"<memory_context>\n{self._assemble()}\n</memory_context>\nUser: {question}"

    # ---------- 内部 ----------

    def _summarize_buffer(self) -> None:
        from atlas.context.extractor import _render_raw  # 同一份记录截断保护（3500/7000）

        raw = _render_raw(self.buffer)
        summary = ""
        try:
            summary = (self.llm(_SUMMARY_INSTRUCTION + raw) or "").strip()
        except Exception as e:
            self.extract_errors.append(f"{type(e).__name__}: {e}")
        if not summary:
            self.fallbacks += 1
            summary = raw[:_FALLBACK_CHARS]
        self.summaries.append(summary)
        self.buffer = []
        self.carry_log.append(f"摘要#{len(self.summaries)}：{summary[:80]}")

    def _assemble(self) -> str:
        header = "## 已完成工作摘要"
        if not self.summaries:
            return f"{header}\n（无）"
        body = "\n".join(
            f"- [摘要 {i}] {s}" for i, s in enumerate(self.summaries, 1)
        )
        return f"{header}\n{body}"
