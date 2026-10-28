"""Task 5 示例：一条完整的研究任务链路（离线可跑，不需要密钥）。

展示五件事，全部只用公开契约（`AgentRuntime` + `ResearchTool`），不碰图引擎内部：

1. 规划：Planner 写 Decision，任务进入 EXECUTING；
2. 研究：调 ResearchTool 取证据 → 结论写成带 provenance 的 Fact；工具失败只记 trace；
3. 执行：Worker 写 Artifact；
4. 核验与修订：Auditor 打回 → 旧产物被 supersede（保留原因）→ 修订后通过；
5. 恢复：任务暂停后，**换一个进程连接**从 checkpoint 继续。

运行：`python examples/research_agent.py`（会在临时目录建库，不污染仓库）。
"""

import json
import os
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from atlas.runtime import AgentRuntime, RuntimeFailure  # noqa: E402
from atlas.service import ServiceRuntime  # noqa: E402
from atlas.storage.repository import GraphRepository, open_database  # noqa: E402
from atlas.tools import EvidenceItem, ResearchRequest, ResearchResult, ResearchToolError  # noqa: E402
from atlas.tools.compassrag import LocalResearchTool  # noqa: E402


def scripted_tool(request: ResearchRequest) -> ResearchResult:
    """本地研究工具替身：真实场景换成 CompassRAG（Local 或 Http 实现同一协议）。"""
    if "无解" in request.query:
        raise ResearchToolError("no_evidence", "库里没有相关文档", trace_id=request.trace_id)
    return ResearchResult(
        answer="常数项为 2τσ²（修订版）",
        evidence=[
            EvidenceItem(source="paper:2312.05134", chunk_id="c12",
                         quote="Theorem 3: L ≤ 2τσ²", score=0.91),
        ],
        confidence=0.86,
        retrieval_rounds=2,
        stop_reason="evidence_sufficient",
        trace_id=request.trace_id,
    )


class RoleWriter:
    """角色写共享图的最小适配器：只经 `ServiceRuntime`，不直接碰 ORM。"""

    def __init__(self, repository: GraphRepository, session_id: str):
        self.runtime = ServiceRuntime(repository, session_id)
        self.session_id = session_id

    def note(self, role: str, kind: str, content: str, *, reason: str = "",
             evidence: str = "", source: str = "", confidence: float = 0.0,
             verified_at: str = "") -> Dict[str, Any]:
        row = self.runtime.note(
            role, content, kind=kind, reason=reason, evidence=evidence,
            source=source, confidence=confidence, verified_at=verified_at,
        )
        return {"node_id": row.node_id}


def _research(writer: RoleWriter, tool, query: str, subtask_id: str = "S1") -> Dict[str, Any]:
    """调工具 → 写 Fact；失败只留痕，不写事实、不改状态机。"""
    request = ResearchRequest(session_id=writer.session_id, subtask_id=subtask_id,
                              query=query)
    try:
        result = tool.research(request)
    except ResearchToolError as exc:
        return {"ok": False, "error": {"kind": exc.kind, "message": exc.message,
                                       "trace_id": request.trace_id}}
    if not result.usable:
        return {"ok": False, "error": {"kind": "no_evidence", "message": "工具没有结论",
                                       "trace_id": request.trace_id}}
    note = writer.note(
        "research", "fact", result.answer,
        reason=f"检索 {result.retrieval_rounds} 轮，confidence={result.confidence:.2f}",
        evidence=result.evidence_json(),
        source=",".join(item.source for item in result.evidence[:3]),
        confidence=result.confidence,
    )
    return {"ok": True, "fact_id": note["node_id"], "result": result}


def run_research_demo(db_path: str, tool=None,
                      llm: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """跑一遍完整链路，返回结构化摘要（测试与命令行共用）。"""
    database = open_database(db_path)
    repository = GraphRepository(database)
    runtime = AgentRuntime(repository)
    writer = RoleWriter(repository, "demo")
    tool = tool or LocalResearchTool(scripted_tool)
    summary: Dict[str, Any] = {"steps": []}

    try:
        # 0) 建任务
        runtime.create_task("demo", "证明 L 的上界并给出常数项", request_id="demo-0",
                        idempotency_key="create-1")
        summary["steps"].append(("create", runtime.get_state("demo")["state"]))

        # 1) 规划
        runtime.advance_task("demo", "plan_ready", reason="先证引理 2，再代递推式",
                         idempotency_key="plan-1")
        writer.note("planner", "decision", "用引理 2 的鞅版本", reason="题设依赖引理 2")
        summary["steps"].append(("plan", runtime.get_state("demo")["state"]))

        # 2) 研究（成功路径）
        research = _research(writer, tool, "引理 2 的鞅版本界是什么？")
        summary["research_ok"] = research["ok"]
        summary["fact_id"] = research.get("fact_id")

        # 3) 执行：Worker 写草稿
        draft = writer.note("worker", "artifact", "证明草稿：由引理 2 得 L ≤ 2τσ²…")
        summary["draft_id"] = draft["node_id"]
        runtime.advance_task("demo", "work_done", reason="草稿完成", idempotency_key="work-1")

        # 4) 核验打回 → 修订（旧草稿被 supersede，原因保留）
        runtime.advance_task("demo", "audit_repair", reason="常数项需用修订版",
                         idempotency_key="repair-1")
        revised = writer.note("worker", "artifact", "修订稿：常数项取 2τσ²", reason="常数项需用修订版")
        writer.runtime.supersede(summary["draft_id"], revised["node_id"], reason="常数项需用修订版")
        runtime.advance_task("demo", "repair_done", reason="修订完成", idempotency_key="repair-2")
        runtime.advance_task("demo", "audit_pass", reason="证据与约束齐备",
                         idempotency_key="pass-1")
        summary["steps"].append(("audit", runtime.get_state("demo")["state"]))

        # 5) 工具失败：只留痕，不写事实
        failed = _research(writer, LocalResearchTool(_failing_tool), "无解的命题")
        summary["failed_research"] = failed

        # 6) 暂停（如等待人工确认），然后模拟进程重启后恢复
        runtime2 = AgentRuntime(repository)
        runtime2.create_task("demo2", "第二件任务", idempotency_key="c2")
        runtime2.advance_task("demo2", "plan_ready", reason="路线", idempotency_key="p2")
        runtime2.pause_task("demo2", reason="等用户补充预算", idempotency_key="pause-2")
        summary["final_state"] = runtime.get_state("demo")["state"]
        database.close()

        # —— 换一个连接（等价于新进程）继续 ——
        database2 = open_database(db_path)
        try:
            runtime3 = AgentRuntime(GraphRepository(database2))
            resumed = runtime3.resume_task("demo2", request_id="resume-1", idempotency_key="resume-2")
            summary["steps"].append(("resume", resumed["state"]))
            summary["replay"] = runtime3.replay("demo2")
        finally:
            database2.close()

        return summary
    finally:
        try:
            database.close()
        except Exception:  # 已经关过
            pass


def _failing_tool(request: ResearchRequest) -> ResearchResult:
    raise ResearchToolError("no_evidence", "库里没有相关文档", trace_id=request.trace_id)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        summary = run_research_demo(os.path.join(tmp, "demo.db"))
    print("== 研究任务链路 ==")
    for step, state in summary["steps"]:
        print(f"  {step:8s} → {state}")
    print(f"  研究成功: {summary['research_ok']}  写出的 Fact: {summary['fact_id']}")
    print(f"  工具失败（无证据）: {summary['failed_research']['error']['kind']} —— 未写事实")
    replay = summary["replay"]
    print(f"  重启后回放: ok={replay['ok']} 业务事件={len(replay['timeline'])} "
          f"trace={len(replay['traces'])}")


if __name__ == "__main__":
    main()
