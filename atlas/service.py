"""MemoryArena 接入形态：图引擎 + 结转 + 组装器的官方 memory 系统等价物（决策 #11–#14）。

节奏（math 域：每子任务一次 wrap + 一次 add）：
- `wrap_user_prompt(q)`：① 若缓冲非空 → 先结转上一子任务（LLM 提取入图 + 叙事摘要）并标记完成；
  ② 注册当前子任务节点（规则，只存 PROBLEM 部分）；③ 组装四区上下文，包成与基线同形的
  `<memory_context>…</memory_context>\nUser: {q}`。
- `add_chunk(chunk)`：追加原始记录到缓冲；结转发生在下一个 wrap 边界（决策 #11：每子任务边界）。

判分口径（决策 #14）：chunk 含 `## Judge: CORRECT/INCORRECT` 时解析并写入子任务 note + 失败标记
（H 区提示）；缺失则视为无判分信息（官方 judge_result_in_memory=false 时的行为）。

组装不变量被破坏（查账失败/计划区溢出/依赖环）时降级为计划区并记录到 `assemble_errors`，
保证评测跑不因单点异常中断；明细留服务日志供事后审计。
"""

import os
import re
from typing import Callable, List, Optional, Sequence

from atlas.context.assembler import ContextAssembler
from atlas.context.carryover import CarryOverManager
from atlas.context.extractor import LLMCarryOverExtractor
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import NodeStatus

DEFAULT_BUDGET_TOKENS = 8000  # 决策 #13
_SUBTASK_CONTENT_LIMIT = 1000
_JUDGE_RE = re.compile(r"##\s*Judge:\s*(CORRECT|INCORRECT)", re.IGNORECASE)


def parse_judge(entries: Sequence[str]) -> Optional[bool]:
    """从原始记录里解析判分结果；无判分信息返回 None。"""
    match = _JUDGE_RE.search("\n".join(entries))
    if not match:
        return None
    return match.group(1).upper() == "CORRECT"


def extract_task_text(question: str) -> str:
    """题面可能带 `### BACKGROUND/### PROBLEM` 结构；子任务节点只存 PROBLEM 部分。"""
    text = question.split("### PROBLEM", 1)[1] if "### PROBLEM" in question else question
    text = text.strip().lstrip(":：").strip()
    if len(text) > _SUBTASK_CONTENT_LIMIT:
        text = text[:_SUBTASK_CONTENT_LIMIT].rstrip() + "…"
    return text


class AtlasMemorySystem:
    def __init__(
        self,
        user_id: str,
        llm: Callable[[str], str],
        token_counter: Callable[[str], int],
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        shares: Optional[dict] = None,
        floors: Optional[dict] = None,
        dump_path: Optional[str] = None,
    ):
        self.user_id = user_id
        self.engine = GraphMemoryEngine()
        self.extractor = LLMCarryOverExtractor(llm)
        self.carry = CarryOverManager(self.engine, token_counter)
        self.assembler = ContextAssembler(
            self.engine, token_counter, shares=shares, floors=floors
        )
        self.budget_tokens = budget_tokens
        self.buffer: List[str] = []
        self.summaries: List[str] = []
        self.pending_subtask: Optional[str] = None
        self.carries = 0
        self.carry_log: List[str] = []      # 每次结转的一行审计（服务端打日志）
        self.extract_errors: List[str] = []
        self.assemble_errors: List[str] = []
        self.dump_errors: List[str] = []
        self.dump_path = dump_path          # 图状态快照路径（决策 #10；None 表示不落盘）

    # ---------- 官方三端点对应的行为 ----------

    def add_chunk(self, chunk: str) -> None:
        self.buffer.append(chunk)

    def wrap_user_prompt(self, question: str) -> str:
        if self.buffer and self.pending_subtask is not None:
            self._carry_over(question)
        elif self.buffer:
            # 首个 wrap 之前的记录（runner 会先 add "Initial result: Empty"）：
            # 尚无子任务可结转，直接丢弃，不烧提取调用。
            self.buffer = []
        self.pending_subtask = self._register_subtask(question).id
        context = self._safe_assemble()
        self._dump()
        return f"<memory_context>\n{context}\n</memory_context>\nUser: {question}"

    # ---------- 内部 ----------

    def _register_subtask(self, question: str):
        return self.engine.add_subtask(extract_task_text(question))

    def _carry_over(self, next_question: str) -> None:
        subtask_id = self.pending_subtask
        correct = parse_judge(self.buffer)
        if subtask_id is not None:
            node = self.engine.nodes[subtask_id]
            if node.status == NodeStatus.ACTIVE:
                self.engine.complete_subtask(subtask_id)
            if correct is True:
                node.note = "Judge: CORRECT"
            elif correct is False:
                node.note = "Judge: INCORRECT"
                node.meta["failed"] = True

        captured: dict = {}

        def _extract(raw, engine):
            result = self.extractor.extract(
                raw, engine, next_question=extract_task_text(next_question)
            )
            captured["result"] = result
            return result

        lines = self.carry.execute(self.buffer, extractor=_extract)
        result = captured.get("result")
        if result is not None:
            self.carry_log.append(
                f"结转#{self.carries + 1}（{subtask_id}）：applied={len(result.ops_applied)} "
                f"skipped={len(result.ops_skipped)} summary={result.summary[:80]}"
            )
            if result.error:
                self.extract_errors.append(result.error)
        else:
            self.carry_log.append(f"结转#{self.carries + 1}（{subtask_id}）：提取器未返回结果")
        self.summaries.extend(lines)
        self.buffer = []
        self.carries += 1
        self.pending_subtask = None

    def _safe_assemble(self) -> str:
        try:
            return self.assembler.assemble(
                self.summaries, self.buffer, self.budget_tokens
            ).text
        except Exception as e:
            # 组装不变量被破坏（查账失败/计划区溢出/依赖环等）：降级为计划区，保证跑分不中断；
            # 明细留在 assemble_errors 里供事后审计（服务端会打日志）。
            self.assemble_errors.append(f"{type(e).__name__}: {e}")
            return self.assembler.render_plan()

    def _dump(self) -> None:
        """图状态快照（决策 #10：HTML/SVG 导出的数据源）；快照失败绝不影响跑分。"""
        if not self.dump_path:
            return
        try:
            os.makedirs(os.path.dirname(self.dump_path), exist_ok=True)
            self.engine.save_jsonl(self.dump_path)
        except Exception as e:
            self.dump_errors.append(f"{type(e).__name__}: {e}")
