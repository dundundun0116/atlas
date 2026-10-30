"""端到端任务级评测（**离线脚手架**）：Atlas 编排 × CompassRAG 检索 × 恢复。

口径声明（与 D9/D11/D12 一致）：

- Atlas 的 Planner/Worker/Auditor 与 CompassRAG 的 LLM 都是**脚本化替身**，
  检索器是关键词替身；本文件度量的是**流水线是否闭合、判分是否有效**，
  **不是效果数字**。真实数字必须：真实模型 + 固定题集/索引版本 + ≥2 次重复。
- 场景覆盖设计文档 §9 阶段 4 要求的四类：多子问题（多来源证据）、来源冲突、
  需要修订、工具失败；另加一条 Atlas 自身的暂停/恢复。

产出：`runs/merged_e2e/summary.json`（gitignored）+ 终端四列表。

用法：`python scripts/run_merged_e2e.py`
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas.orchestration import Orchestrator, TaskState, TaskStateMachine
from atlas.runtime import AgentRuntime
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import CompassRAGAgentTool
from compassrag.agent import Agent as CompassAgent
from compassrag.agent import AgentConfig
from compassrag.llm.client import LLMResponse

# --------------------------------------------------------------------------- #
# 语料与替身
# --------------------------------------------------------------------------- #

CHUNKS: dict[str, dict] = {
    "c1": {"chunk_id": "c1", "title": "Albert Einstein",
           "text": "爱因斯坦 1879 年出生于德国乌尔姆。"},
    "c2": {"chunk_id": "c2", "title": "狭义相对论",
           "text": "狭义相对论由爱因斯坦于 1905 年提出。"},
    "c3": {"chunk_id": "c3", "title": "传记 A",
           "text": "爱因斯坦出生于 1879 年。"},
    "c4": {"chunk_id": "c4", "title": "传记 B",
           "text": "另有资料记载爱因斯坦出生于 1880 年，与主流说法冲突。"},
    "c5": {"chunk_id": "c5", "title": "上界定理",
           "text": "定理 3：L ≤ 2τσ²。"},
}


class KeywordRetriever:
    """关键词命中的确定性检索器：只按给定 ranking 返回，用来锁定证据集。"""

    def __init__(self, ranking):
        self.ranking = list(ranking)

    def search(self, query, k=10):
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class FakeLLM:
    """CompassRAG 的 LLM 替身：恒定答案（非空，避免触发空答案重试）。"""

    def __init__(self, answer: str):
        self.answer = answer or "unknown"
        self.calls = 0

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls += 1
        return LLMResponse(text=self.answer, model="scripted", prompt_tokens=10,
                           completion_tokens=3, total_tokens=13, latency_ms=1.0,
                           finish_reason="stop")


class ScriptedAtlasLLM:
    """Atlas 编排的脚本化 LLM：按队列返回 Planner / Worker / Auditor / 修订。"""

    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt: str) -> str:
        if not self.replies:
            return json.dumps({"verdict": "ok", "issues": ""})
        return self.replies.pop(0)


# --------------------------------------------------------------------------- #
# 场景定义
# --------------------------------------------------------------------------- #


def _plan(route: str) -> str:
    return json.dumps({"route": route, "risks": "证据可能冲突", "reason": "事实型问题"})


_AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})


@dataclass
class Scenario:
    name: str
    question: str
    ranking: list[str]
    compass_answer: str
    atlas_replies: list[str]
    expected_chunks: set[str] = field(default_factory=set)
    expect_research: bool = True
    expect_revision: bool = False
    expect_tool_failure: bool = False
    expect_conflict: bool = False


SCENARIOS: list[Scenario] = [
    Scenario(
        name="multi_evidence",
        question="爱因斯坦出生于哪一年，又是在哪一年提出狭义相对论？",
        ranking=["c1", "c2"],
        compass_answer="1879 年出生；1905 年提出狭义相对论。",
        atlas_replies=[_plan("分两步核对出生年与相对论年份"), "1879 年 / 1905 年", _AUDIT_OK],
        expected_chunks={"c1", "c2"},
    ),
    Scenario(
        name="source_conflict",
        question="爱因斯坦的出生年份是哪一年？",
        ranking=["c3", "c4"],
        compass_answer="来源冲突：1879 年与 1880 年两种记载，未自动择一。",
        atlas_replies=[_plan("核对两个来源"), "存在冲突：1879 / 1880", _AUDIT_OK],
        expected_chunks={"c3", "c4"},
        expect_conflict=True,
    ),
    Scenario(
        name="needs_revision",
        question="给出 L 的上界常数项。",
        ranking=["c5"],
        compass_answer="L ≤ 2τσ²。",
        atlas_replies=[
            _plan("先查定理 3"),
            "草稿：常数项为 2τ（漏掉 σ²）",
            json.dumps({"verdict": "repair_required", "issues": "常数项漏了 σ²"}),
            "修订稿：常数项为 2τσ²（依据定理 3）",
        ],
        expected_chunks={"c5"},
        expect_revision=True,
    ),
    Scenario(
        name="tool_failure",
        question="查询一个语料里不存在的问题。",
        ranking=[],
        compass_answer="unknown",
        atlas_replies=[_plan("直接检索"), "草稿：未获得证据，按未知处理", _AUDIT_OK],
        expect_research=False,
        expect_tool_failure=True,
    ),
]


# --------------------------------------------------------------------------- #
# 图适配器：Orchestrator ↔ 真实仓库
# --------------------------------------------------------------------------- #


class RepositoryGraph:
    """Orchestrator 需要的最小图接口；写路径全部落 repository，不另建内存真源。"""

    def __init__(self, repository: GraphRepository, session_id: str):
        self.repository = repository
        self.session_id = session_id
        self.service = ServiceRuntime(repository, session_id)

    def state_view(self, role: str) -> str:
        nodes = [n for n in self.repository.nodes(self.session_id) if n.status == "active"]
        lines = [f"[{role}] 活跃节点 {len(nodes)} 个"]
        lines += [f"- {n.type}: {str(n.content)[:60]}" for n in nodes[-5:]]
        return "\n".join(lines)

    def role_note(self, role, kind, content, reason="", evidence="", source="", confidence=0.0,
                  meta=None):
        if kind == "artifact_revise":
            drafts = [n for n in self.repository.nodes(self.session_id)
                      if n.type == "artifact" and n.actor == role and n.status == "active"]
            row = self.service.note(role, content, kind="artifact", reason=reason,
                                    evidence=evidence, source=source, confidence=confidence,
                                    meta=meta)
            if drafts:
                self.service.supersede(drafts[-1].node_id, row.node_id,
                                       reason=reason or "auditor 打回后修订")
            return {"node_id": row.node_id, "kind": kind, "actor": role}
        row = self.service.note(role, content, kind=kind, reason=reason,
                                evidence=evidence, source=source, confidence=confidence,
                                meta=meta)
        return {"node_id": row.node_id, "kind": kind, "actor": role}


def build_research_tool(scenario: Scenario) -> CompassRAGAgentTool:
    agent = CompassAgent(
        FakeLLM(scenario.compass_answer),
        {"bm25": KeywordRetriever(scenario.ranking)},
        CHUNKS,
        AgentConfig(top_k=5),
    )
    return CompassRAGAgentTool(agent)


# --------------------------------------------------------------------------- #
# 跑场景
# --------------------------------------------------------------------------- #


def run_scenario(scenario: Scenario, db_path: Path) -> dict[str, Any]:
    database = open_database(db_path)
    repository = GraphRepository(database)
    session = scenario.name
    repository.create_session(session, goal=scenario.question)
    graph = RepositoryGraph(repository, session)
    machine = TaskStateMachine()
    tool = build_research_tool(scenario)
    try:
        orchestrator = Orchestrator(
            ScriptedAtlasLLM(scenario.atlas_replies), graph,
            machine=machine, research_tool=tool if scenario.expect_research else tool,
        )
        answer, trace = orchestrator.solve(
            scenario.question, session_id=session, subtask_id="S1",
            research_query=scenario.question,
        )
        nodes = repository.nodes(session)
        research_facts = [n for n in nodes if n.type == "fact" and n.actor == "research"]
        facts = [n for n in nodes if n.type == "fact"]
        # 只考核研究结论的 provenance；auditor 的"疑虑"本来就是无外部证据的图内记录。
        unsupported = [n for n in research_facts if not (n.evidence or "").strip()]
        evidence_chunks: set[str] = set()
        evidence_sources: set[str] = set()
        for fact in research_facts:
            for item in json.loads(fact.evidence or "[]"):
                evidence_chunks.add(item.get("chunk_id", ""))
                evidence_sources.add(item.get("source", ""))
        artifacts = [n for n in nodes if n.type == "artifact"]
        superseded = [n for n in artifacts if n.status == "superseded"]
        active_artifacts = [n for n in artifacts if n.status == "active"]
        return {
            "name": scenario.name,
            "state": machine.state.value,
            "completed": machine.state is TaskState.COMPLETED and bool(answer.strip()),
            "answer": answer,
            "research_fact_count": len(research_facts),
            "fact_count": len(facts),
            "unsupported_fact_count": len(unsupported),
            "evidence_chunks": sorted(evidence_chunks),
            "evidence_sources": sorted(evidence_sources),
            "coverage_ok": scenario.expected_chunks.issubset(evidence_chunks),
            "research_error_kind": (trace.research_error or {}).get("kind", ""),
            "research_trace_id": trace.research_trace_id,
            "superseded_artifacts": len(superseded),
            "active_artifacts": len(active_artifacts),
            "revision_ok": bool(superseded) and bool(active_artifacts),
            "conflict_ok": (len(evidence_sources) >= 2) if scenario.expect_conflict else None,
            "tool_failure_ok": (
                None if not scenario.expect_tool_failure else
                (not research_facts
                 and (trace.research_error or {}).get("kind") in ("no_evidence", "budget_exhausted"))
            ),
        }
    finally:
        database.close()


def run_recovery(db_path: Path) -> dict[str, Any]:
    """Atlas 自身：PAUSED → 换连接恢复一次；重复恢复应被拒绝。"""
    from atlas.runtime import RuntimeFailure

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    database = open_database(db_path)
    repository = GraphRepository(database)
    try:
        runtime = AgentRuntime(repository)
        runtime.create_task("recovery", "恢复演练", idempotency_key="create-1")
        runtime.advance_task("recovery", "plan_ready", reason="路线已定", idempotency_key="plan-1")
        runtime.pause_task("recovery", reason="模拟进程中断", idempotency_key="pause-1")
        nodes_before = len(repository.nodes("recovery"))

        # 换一个连接 = 新进程视角
        resumed = AgentRuntime(repository).resume_task("recovery", idempotency_key="resume-1")
        once = resumed["state"]

        twice = ""
        try:
            AgentRuntime(repository).resume_task("recovery", idempotency_key="resume-2")
            twice = "unexpected-success"
        except RuntimeFailure as exc:
            twice = getattr(exc, "kind", type(exc).__name__)

        return {
            "name": "recovery",
            "completed": once == "executing",
            "state_after_resume": once,
            "second_resume": twice,
            "recovered_once": once == "executing" and twice != "unexpected-success",
            "fact_count_unchanged": len(repository.nodes("recovery")) == nodes_before,
        }
    finally:
        database.close()


def run_all(output_dir: str | Path | None = None) -> dict[str, Any]:
    output = Path(output_dir) if output_dir else Path("runs/merged_e2e")
    with tempfile.TemporaryDirectory() as tmp:
        scenario_results = [run_scenario(s, Path(tmp) / f"{s.name}.db") for s in SCENARIOS]
        recovery = run_recovery(Path(tmp) / "recovery.db")

    tasks = len(scenario_results)
    research_tasks = [r for r in scenario_results if r["research_fact_count"] > 0]
    conflict_tasks = [r for r in scenario_results if r["conflict_ok"] is not None]
    aggregate = {
        "tasks": tasks,
        "completed": sum(1 for r in scenario_results if r["completed"]),
        "research_tasks": len(research_tasks),
        "coverage_ok": sum(1 for r in scenario_results if r["coverage_ok"]),
        "fact_with_evidence": sum(1 for r in research_tasks if r["research_fact_count"] > 0),
        "unsupported_facts": sum(r["unsupported_fact_count"] for r in scenario_results),
        "conflict_preserved": sum(1 for r in conflict_tasks if r["conflict_ok"]),
        "conflict_tasks": len(conflict_tasks),
        "revision_ok": sum(1 for r in scenario_results if r["revision_ok"]),
        "tool_failure_isolated": sum(1 for r in scenario_results if r["tool_failure_ok"] is True),
        "tool_failure_tasks": sum(1 for r in scenario_results if r["tool_failure_ok"] is not None),
        "recovery_ok": int(recovery["recovered_once"]),
        "duplicate_facts": sum(max(0, r["research_fact_count"] - 1) for r in research_tasks),
    }

    summary = {
        "mode": "offline",
        "models": {
            "atlas": "scripted (Planner/Worker/Auditor 固定回复)",
            "compassrag": "scripted LLM + keyword retriever",
        },
        "note": "离线脚手架：只验证流水线与判分机制，不是效果数字；"
                "真实结论需要真实模型 + 固定题集/索引 + 重复。",
        "aggregate": aggregate,
        "scenarios": scenario_results + [recovery],
    }
    if output_dir is not False:
        output.mkdir(parents=True, exist_ok=True)
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return summary


def format_table(summary: dict[str, Any]) -> str:
    agg = summary["aggregate"]
    lines = [
        f"模式：{summary['mode']}（{summary['note']}）",
        "",
        "| 场景 | 完成 | 研究 Fact | 证据覆盖 | 冲突保留 | 修订 |",
        "|---|---|---|---|---|---|",
    ]
    for r in summary["scenarios"]:
        if r["name"] == "recovery":
            continue
        conflict = "—" if r["conflict_ok"] is None else str(r["conflict_ok"])
        lines.append(
            f"| {r['name']} | {r['completed']} | {r['research_fact_count']} | "
            f"{r['coverage_ok']} | {conflict} | {r['revision_ok']} |"
        )
    lines += [
        "",
        f"任务 {agg['completed']}/{agg['tasks']} 完成；研究 Fact 带证据 "
        f"{agg['fact_with_evidence']}/{agg['research_tasks']}；"
        f"无证据 Fact {agg['unsupported_facts']}；工具失败隔离 "
        f"{bool(agg['tool_failure_isolated'])}；冲突保留 {agg['conflict_preserved']}/"
        f"{agg['conflict_tasks']}；恢复 {bool(agg['recovery_ok'])}",
    ]
    return "\n".join(lines)
