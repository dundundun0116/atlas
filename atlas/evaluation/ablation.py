"""DAG 路径消融：同题集下 `plan_dag_v1` 与 `legacy_research_v1` 的对照（设计文档 §11 Phase 5）。

要回答的问题很具体：**把研究流程迁成显式 DAG，代价与收益分别是什么？**

- 收益侧：崩溃能续跑、未知结果能对账、下游失效有记录（这些只有 DAG 有）；
- 代价侧：更多一次模型调用？更多工具调用？更慢？

所以这里在同一批**由种子生成的题**上跑两条路径，逐题比对：
任务成功（oracle 判）、工具调用次数、模型调用次数、终态。
**两条路径的差距只在机制上，不在题目上**——题集、语料、判分完全是同一份。

诚实边界：研究者是脚本化的，度量的是"机制开销"，不是"效果差异"；
`legacy_research_v1` 没有恢复入口，所以"恢复"那一列对它永远是"—"，不是 0。
"""

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from atlas.api import create_app
from atlas.evaluation.longhorizon import (  # noqa: F401 - generate_tasks 供调用方复用
    AnswerOracle,
    FaultCounter,
    GeneratedTask,
    ScriptedReader,
    SuiteResearchTool,
    generate_tasks,
    plan_faults,
)
from atlas.storage.repository import GraphRepository, open_database

DAG_WORKFLOW = "plan_dag_v1"
LEGACY_WORKFLOW = "legacy_research_v1"


@dataclass
class WorkflowRun:
    """一条工作流在一道题上的结果。"""

    workflow: str
    task_id: str
    state: str = ""
    answer: str = ""
    passed: bool = False
    reason: str = ""
    tool_calls: int = 0
    model_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"workflow": self.workflow, "task_id": self.task_id,
                "state": self.state, "answer": self.answer, "passed": self.passed,
                "reason": self.reason, "tool_calls": self.tool_calls,
                "model_calls": self.model_calls}


@dataclass
class AblationReport:
    tasks: List[GeneratedTask]
    runs: List[WorkflowRun] = field(default_factory=list)

    def by_workflow(self, workflow: str) -> List[WorkflowRun]:
        return [run for run in self.runs if run.workflow == workflow]

    def summary(self, workflow: str) -> Dict[str, Any]:
        runs = self.by_workflow(workflow)
        if not runs:
            return {}
        return {
            "workflow": workflow,
            "n": len(runs),
            "passed": sum(1 for run in runs if run.passed),
            "tool_calls": sum(run.tool_calls for run in runs),
            "model_calls": sum(run.model_calls for run in runs),
            "completed": sum(1 for run in runs if run.state == "completed"),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": len(self.tasks),
            "summaries": {"dag": self.summary(DAG_WORKFLOW),
                          "legacy": self.summary(LEGACY_WORKFLOW)},
            "runs": [run.to_dict() for run in self.runs],
        }

    def to_markdown(self) -> str:
        dag, legacy = self.summary(DAG_WORKFLOW), self.summary(LEGACY_WORKFLOW)
        lines = ["# DAG 路径消融（同题集）", ""]
        lines.append(f"- 题数：{len(self.tasks)}；语料、判分、研究者完全相同，只换工作流")
        lines += ["", "| 指标 | plan_dag_v1 | legacy_research_v1 |", "| --- | --- | --- |"]
        for key, label in (("passed", "任务成功（oracle）"), ("completed", "到终态"),
                           ("tool_calls", "工具调用次数"), ("model_calls", "模型调用次数")):
            lines.append(f"| {label} | {dag.get(key, '—')} | {legacy.get(key, '—')} |")
        lines.append("| 恢复入口 | 有（recover） | **没有** |")
        lines += ["", "说明：研究者是脚本化的，这里的差距是**机制开销**，不是效果差异；"
                      "legacy 路径没有恢复入口，所以恢复一列对它是「—」而不是 0。"]
        return "\n".join(lines)


class DualReader(ScriptedReader):
    """消融专用研究者：两条工作流**用同一个**脚本化研究者。

    legacy 路径先要一份 JSON 计划（PLANNER），DAG 路径不需要——所以这里补上计划分支，
    其余（AUDITOR 判 ok、从 `ANSWER:<n>` 抄答案）与 `ScriptedReader` 完全一致。
    一致性很重要：两条路径要是换了研究者，比出来的就不是工作流的差别了。
    """

    PLAN = ('{"route": "先检索证据再作答", "steps": ["检索证据", "作答", "核验"],'
            ' "reason": "先取证再作答"}')

    def __call__(self, prompt: str) -> str:
        # 精确匹配角色行：WORKER 的提示里也会出现 "PLANNER" 字样（引用计划），
        # 粗匹配会让 worker 拿到一份 JSON 计划、把答案写成计划本身。
        if "You are the PLANNER" in prompt:
            self.calls += 1
            return self.PLAN
        return super().__call__(prompt)


def _grade(answer: str, task: GeneratedTask) -> tuple:
    verdict = AnswerOracle().grade(answer, task)
    return verdict.passed, verdict.reason


def _run_dag(client, registry, task: GeneratedTask, session_id: str) -> WorkflowRun:
    run = WorkflowRun(workflow=DAG_WORKFLOW, task_id=task.task_id)
    client.post("/tasks", json={"session_id": session_id, "goal": task.question})
    client.post(f"/tasks/{session_id}/plans",
                json={"problem": task.question, "activate": True})
    body = client.post(f"/tasks/{session_id}/plans/P1/advance", json={}).json()
    run.state = body.get("state", "")
    run.answer = _latest_draft(registry, session_id)
    run.passed, run.reason = _grade(run.answer, task)
    run.tool_calls = len(registry.tool_calls(session_id))
    return run


def _latest_draft(registry, session_id: str, plan_id: str = "P1") -> str:
    """判分读的是**产物**：草稿步最后一次成功 attempt 的输出。"""
    drafts = [item for item in registry.attempts(session_id, plan_id, "draft")
              if item["status"] == "succeeded"]
    if not drafts:
        return ""
    return str((drafts[-1]["output"] or {}).get("draft") or "")


def _run_legacy(client, registry, task: GeneratedTask, session_id: str) -> WorkflowRun:
    run = WorkflowRun(workflow=LEGACY_WORKFLOW, task_id=task.task_id)
    client.post("/tasks", json={"session_id": session_id, "goal": task.question})
    body = client.post(f"/tasks/{session_id}/orchestrate",
                       json={"problem": task.question}).json()
    run.state = body.get("state", "")
    run.answer = str(body.get("draft") or body.get("answer") or "")
    run.passed, run.reason = _grade(run.answer, task)
    run.tool_calls = 1 if body.get("fact_ids") else 0
    return run


def run_ablation(tasks: Sequence[GeneratedTask]) -> AblationReport:
    """同一批题跑两条工作流，逐题记录。"""
    report = AblationReport(tasks=list(tasks))
    counter = FaultCounter(planned={})
    tool = SuiteResearchTool(tasks, plan_faults(tasks, every=0), counter)
    # 用临时**文件**库：`:memory:` 在 TestClient 的线程里各是一片，表建不到一起
    with tempfile.TemporaryDirectory() as tmp:
        database = open_database(os.path.join(tmp, "ablation.db"))
        registry = GraphRepository(database)

        def client_and_reader():
            from fastapi.testclient import TestClient

            reader = DualReader()
            app = create_app(
                registry, research_tool=tool,
                orchestrate_llm=reader,
                context_token_counter=lambda text: (len(text) + 3) // 4,
            )
            return TestClient(app), reader

        dag_client, dag_reader = client_and_reader()
        legacy_client, legacy_reader = client_and_reader()

        for index, task in enumerate(tasks):
            before = dag_reader.calls
            run = _run_dag(dag_client, registry, task, f"dag-{index:02d}")
            run.model_calls = dag_reader.calls - before
            report.runs.append(run)

            before = legacy_reader.calls
            legacy = _run_legacy(legacy_client, registry, task, f"legacy-{index:02d}")
            legacy.model_calls = legacy_reader.calls - before
            report.runs.append(legacy)
        database.close()
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - 薄入口
    import argparse

    parser = argparse.ArgumentParser(description="DAG 路径消融（同题集）")
    parser.add_argument("--tasks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_ablation(generate_tasks(count=args.tasks, seed=args.seed))
    print(report.to_markdown())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
