"""MemoryArena 接入形态：图引擎 + 结转 + 组装器的官方 memory 系统等价物。

节奏（math 域：每子任务一次 wrap + 一次 add）：
- `wrap_user_prompt(q)`：① 若缓冲非空 → 先结转上一子任务（LLM 提取入图 + 叙事摘要）并标记完成；
  ② 注册当前子任务节点（规则，只存 PROBLEM 部分）；③ 组装四区上下文，包成与基线同形的
  `<memory_context>…</memory_context>\nUser: {q}`。
- `add_chunk(chunk)`：追加原始记录到缓冲；结转发生在下一个 wrap 边界。

判分口径：chunk 含 `## Judge: CORRECT/INCORRECT` 时解析并写入子任务 note + 失败标记
（H 区提示）；缺失则视为无判分信息（官方 judge_result_in_memory=false 时的行为）。

组装不变量被破坏（查账失败/计划区溢出/依赖环）时降级为计划区并记录到 `assemble_errors`，
保证评测跑不因单点异常中断；明细留服务日志供事后审计。
"""

import json
import os
import re
from typing import Callable, List, Optional, Sequence

from atlas.context.assembler import ContextAssembler
from atlas.context.carryover import CarryOverManager
from atlas.context.extractor import LLMCarryOverExtractor
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import NodeStatus, NodeType
from atlas.orchestration.state_machine import TaskStateMachine

DEFAULT_BUDGET_TOKENS = 8000  # 上下文组装总预算
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
        self.dump_path = dump_path          # 图状态快照路径
        self._last_draft_artifact: Optional[str] = None  # 供 auditor 打回后的修订作废

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

    # ---------- 多智能体编排的图接口 ----------

    def known_ids(self) -> set[str]:
        """图里真实存在的节点 id：Auditor 引用校验用（拿得到就强制，不拿不到才宽松）。"""
        return set(self.engine.nodes)

    def state_view(self, role: str) -> str:
        """给角色的图状态视图（计划区 + 踩坑提示）：角色据此协调，而非读对话历史。"""
        view = self.assembler.render_plan()
        hints = self.engine.hints()
        if hints:
            view += "\n\n## 踩坑与提示\n" + "\n".join(hints)
        return view

    def role_note(self, role: str, kind: str, content: str, reason: str = "",
                  evidence: str = "") -> dict:
        """把角色的结论/疑虑写入图（actor=role）。

        kind：decision（Planner 路线，必须带 reason）/ fact（Auditor 疑虑）/
        artifact（Worker 草稿）/ artifact_revise（修订：作废上一草稿并记原因）。
        """
        content = str(content or "").strip()
        if not content:
            raise ValueError("role_note content 不能为空")
        if kind == "decision":
            if not str(reason or "").strip():
                raise ValueError("role_note decision 必须带 reason")
            node = self.engine.add_decision(content, reason=reason, evidence=evidence, actor=role)
        elif kind == "fact":
            node = self.engine.add_fact(content, note=str(reason or ""), actor=role)
        elif kind == "artifact":
            node = self.engine.add_artifact(content, actor=role)
            self._last_draft_artifact = node.id
        elif kind == "artifact_revise":
            previous = self._last_draft_artifact
            if previous and previous in self.engine.nodes and \
                    self.engine.nodes[previous].status == NodeStatus.ACTIVE:
                node = self.engine.supersede_with(
                    previous, new_content=content,
                    reason=reason or "auditor 打回后修订", actor=role,
                )
            else:
                node = self.engine.add_artifact(content, actor=role)
            self._last_draft_artifact = node.id
        else:
            raise ValueError(f"不支持的 role_note kind: {kind}")
        self._dump()
        return {"node_id": node.id, "kind": kind, "actor": role}

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
        """图状态快照；快照失败绝不影响跑分。"""
        if not self.dump_path:
            return
        try:
            os.makedirs(os.path.dirname(self.dump_path), exist_ok=True)
            self.engine.save_jsonl(self.dump_path)
        except Exception as e:
            self.dump_errors.append(f"{type(e).__name__}: {e}")


class ServiceRuntime:
    """阶段 4 的服务化外壳：会话 + 幂等写入 + checkpoint / 恢复。

    与 `AtlasMemorySystem` 的分工：后者是 MemoryArena 的 memory-system 适配器（每子任务
    结转、四区组装）；这里只管「状态怎么落库、怎么恢复」，不碰评测协议。

    用法：
        runtime = ServiceRuntime(repository, "s1", goal="规划杭州 3 日行")
        runtime.note("planner", "路线：订票 → 订酒店", kind="decision", reason="先远后近")
        runtime.checkpoint(pending_role="auditor", task_state="auditing")
        # 进程重启后
        runtime = ServiceRuntime(repository, "s1")
        state = runtime.resume()      # 图与 checkpoint 一起回来
    """

    _KINDS = {
        "goal": NodeType.GOAL,
        "subtask": NodeType.SUBTASK,
        "constraint": NodeType.CONSTRAINT,
        "decision": NodeType.DECISION,
        "artifact": NodeType.ARTIFACT,
        "fact": NodeType.FACT,
    }

    def __init__(self, repository, session_id: str, goal: Optional[str] = None,
                 actor: str = "main", machine: Optional[TaskStateMachine] = None):
        self.repository = repository
        self.session_id = session_id
        self.actor = actor
        self.machine = machine
        if goal is not None:
            self.repository.create_session(session_id, goal=goal, actor=actor)
        self.engine = self.repository.rebuild_engine(session_id)

    # ---------- 写入 ----------

    def note(
        self,
        role: str,
        content: str,
        kind: str = "fact",
        *,
        reason: str = "",
        evidence: str = "",
        meta: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
        source: str = "",
        confidence: float = 0.0,
        valid_until: str = "",
        verified_at: str = "",
    ):
        """角色结论/疑虑写入共享图（协调全经图，不传对话历史）。"""
        if kind not in self._KINDS:
            raise ValueError(f"未知节点类型：{kind}，可选 {sorted(self._KINDS)}")
        row = self.repository.add_node(
            self.session_id,
            type_=self._KINDS[kind],
            content=content,
            actor=role,
            reason=reason,
            evidence=evidence,
            meta=meta,
            idempotency_key=idempotency_key,
            source=source,
            confidence=confidence,
            valid_until=valid_until,
            verified_at=verified_at,
        )
        self.engine = self.repository.rebuild_engine(self.session_id)
        return row

    def link(self, src, dst, type_, actor: Optional[str] = None):
        edge = self.repository.link(
            self.session_id, src, dst, type_, actor=actor or self.actor
        )
        self.engine = self.repository.rebuild_engine(self.session_id)
        return edge

    def supersede(self, old, new, reason: str = ""):
        self.repository.supersede(self.session_id, old, new, reason=reason, actor=self.actor)
        self.engine = self.repository.rebuild_engine(self.session_id)

    def expire(self, node_id, reason: str = ""):
        self.repository.expire(self.session_id, node_id, reason=reason, actor=self.actor)
        self.engine = self.repository.rebuild_engine(self.session_id)

    # ---------- checkpoint / 恢复 ----------

    def checkpoint(
        self,
        pending_role: str,
        task_state: Optional[str] = None,
        retry_count: int = 0,
        context_digest_version: str = "",
    ):
        """存检查点；给了状态机就自动带上它的状态与快照。"""
        resolved_state = task_state or (
            self.machine.state.value if self.machine is not None else "running"
        )
        digest = context_digest_version
        if self.machine is not None and not digest:
            digest = json.dumps(self.machine.to_checkpoint(), ensure_ascii=False)
        return self.repository.save_checkpoint(
            self.session_id,
            task_state=resolved_state,
            graph_version=len(self.repository.nodes(self.session_id)),
            pending_role=pending_role,
            retry_count=retry_count,
            context_digest_version=digest,
        )

    def known_ids(self) -> set[str]:
        """本会话在 repository 里的真实节点 id，供编排器做严格引用校验。"""
        return {node.node_id for node in self.repository.nodes(self.session_id)}

    def state_view(self) -> dict:
        """状态视图：状态机 + 图 + 最近 checkpoint（API 与 trace 共用）。"""
        row = self.repository.latest_checkpoint(self.session_id)
        return {
            "state": self.machine.state.value if self.machine is not None else "unknown",
            "graph_version": len(self.repository.nodes(self.session_id)),
            "checkpoint": (
                {
                    "task_state": row.task_state,
                    "graph_version": row.graph_version,
                    "pending_role": row.pending_role,
                    "retry_count": row.retry_count,
                    "context_digest_version": row.context_digest_version,
                    "created_at": row.created_at,
                }
                if row is not None
                else None
            ),
        }

    def resume(self) -> dict:
        """从最新 checkpoint 恢复：图重建 + 待执行角色/状态/版本。"""
        row = self.repository.latest_checkpoint(self.session_id)
        self.engine = self.repository.rebuild_engine(self.session_id)
        if row is not None and row.context_digest_version:
            try:
                self.machine = TaskStateMachine.from_checkpoint(
                    json.loads(row.context_digest_version)
                )
            except (ValueError, TypeError):
                pass  # 摘要版本不是状态机快照：保持原样，不猜
        if row is None:
            return {
                "task_state": "new",
                "pending_role": "",
                "graph_version": 0,
                "retry_count": 0,
                "context_digest_version": "",
            }
        return {
            "task_state": row.task_state,
            "pending_role": row.pending_role,
            "graph_version": row.graph_version,
            "retry_count": row.retry_count,
            "context_digest_version": row.context_digest_version,
        }
