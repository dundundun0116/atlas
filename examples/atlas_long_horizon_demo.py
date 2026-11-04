"""长程演示：一条命令跑完「计划 → 写入 → 崩溃 → 恢复」（离线、零密钥）。

跑法：`python examples/atlas_long_horizon_demo.py`

它把这一轮做出的机制串起来，四个场景各回答一个问题：

1. **研究 + 写入正常完成**：DAG 两步走完，任务到终态；
2. **检索超时（结果未知）**：工具调用落 `indeterminate`、步骤 `needs_human`、
   任务 `paused`——此时**恢复评估会说"必须先对账"**，而不是硬着头皮再跑；
3. **写入部分生效**：`failed_partial` → **自动补偿** → 外部世界复原 → 任务失败但没留残渣；
4. **崩溃后恢复**：跑到一半进程没了，`recover` 只补未完成的那几步。

全部用替身（脚本化 LLM / 假检索工具 / 可控写工具），不碰网络也不烧 token。
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from atlas.api import create_app  # noqa: E402
from atlas.planning.write_workflow import build_write_plan  # noqa: E402
from atlas.storage.repository import GraphRepository, open_database  # noqa: E402
from atlas.tools import EvidenceItem, ResearchResult  # noqa: E402
from atlas.tools.protocol import ResearchToolError  # noqa: E402
from atlas.tools.writes import ControllableWriteTool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PROBLEM = "爱因斯坦出生于哪一年？"
AUDIT = json.dumps({"verdict": "ok", "issues": "", "refs": ["c1"]})


class ScriptedLLM:
    def __call__(self, prompt):
        if "revising your draft" in prompt:
            return "修订稿"
        return AUDIT if "AUDITOR" in prompt else "爱因斯坦出生于 1879 年。"


class ScriptedResearchTool:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def research(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ResearchResult(
            answer="1879 年",
            evidence=[EvidenceItem(source="Albert Einstein", chunk_id="c1",
                                   quote="1879 年出生于乌尔姆。", score=0.9,
                                   retriever="bm25", rank=1)],
            confidence=0.8, retrieval_rounds=1, stop_reason="answered",
            trace_id="compass-trace-demo",
        )


def _client(path, *, tool, writer=None):
    database = open_database(path)
    app = create_app(
        GraphRepository(database), research_tool=tool, write_tool=writer,
        orchestrate_llm=ScriptedLLM(),
        context_token_counter=lambda text: (len(text) + 3) // 4,
    )
    return TestClient(app), database


def _title(text):
    print("\n" + "=" * 66)
    print(text)
    print("=" * 66)


def scenario_research_and_write(tmp):
    """1) 研究 + 写入：两条 DAG 都跑到终态。"""
    _title("场景 1 · 研究 + 写入正常完成")
    tool = ScriptedResearchTool()
    writer = ControllableWriteTool(mode="ok")
    client, db = _client(os.path.join(tmp, "s1.db"), tool=tool, writer=writer)

    client.post("/tasks", json={"session_id": "r1", "goal": PROBLEM})
    client.post("/tasks/r1/plans", json={"problem": PROBLEM, "activate": True})
    research = client.post("/tasks/r1/plans/P1/advance", json={}).json()
    steps = {step["step_id"]: step["status"] for step in research["steps"]}
    print(f"研究：state={research['state']} complete={research['complete']} steps={steps}")
    print(f"工具调用：{[(c['tool_name'], c['status']) for c in db and GraphRepository(db).tool_calls('r1')]}")

    client.post("/tasks", json={"session_id": "w1", "goal": "写入订单"})
    payload = build_write_plan(target="order-1",
                               value={"amount": 100, "currency": "CNY"}).to_dict()
    for step in payload["steps"]:
        if step["step_id"] == "write":
            step["tool_args_template"] = {"target": "order-1",
                                          "value": {"amount": 100, "currency": "CNY"}}
        if step["step_id"] == "confirm":
            step["tool_args_template"] = {"target": "order-1"}
    client.post("/tasks/w1/plans", json={"plan": payload, "activate": True})
    write = client.post("/tasks/w1/plans/P1/advance", json={}).json()
    print(f"写入：state={write['state']} 外部世界={writer.world}")
    db.close()


def scenario_unknown_then_recover(tmp):
    """2) 检索超时 → 必须先对账 → 对账后才续跑。"""
    _title("场景 2 · 检索超时：先对账，才允许继续")
    tool = ScriptedResearchTool(error=ResearchToolError("timeout", "上游超时"))
    client, db = _client(os.path.join(tmp, "s2.db"), tool=tool)

    client.post("/tasks", json={"session_id": "r2", "goal": PROBLEM})
    client.post("/tasks/r2/plans", json={"problem": PROBLEM, "activate": True})
    first = client.post("/tasks/r2/plans/P1/advance", json={}).json()
    print(f"第一次推进：state={first['state']} needs_human={first['needs_human']} "
          f"（工具只被调用 {tool.calls} 次）")

    dry = client.post("/tasks/r2/plans/P1/recover", json={"dry_run": True}).json()
    print(f"恢复评估（dry_run）：action={dry['action']} blocked={dry['blocked']}")
    print(f"  原因：{dry['reason']}")

    blocked = client.post("/tasks/r2/plans/P1/recover", json={}).json()
    print(f"强行恢复：executed={blocked['executed']}（有在途未知调用时不动手）")

    calls = GraphRepository(db).tool_calls("r2")
    call_id = calls[0]["tool_call_id"]
    recovered_result = {
        "refs": [{"source": "Albert Einstein", "chunk_id": "c1",
                  "quote": "1879 年出生于乌尔姆。", "score": 0.9,
                  "retriever": "bm25", "rank": 1}],
        "answer": "1879 年", "stop_reason": "answered",
        "research_trace_id": "compass-trace-demo",
    }
    fixed = client.post(f"/tasks/r2/tool-calls/{call_id}/reconcile",
                        json={"outcome": "applied", "result": recovered_result,
                              "detail": "人工确认检索其实返回了"}).json()
    print(f"对账：status={fixed['status']} outcome={fixed['outcome']}"
          f"（查回的结果已落库）")

    resumed = client.post("/tasks/r2/plans/P1/recover",
                          json={"reopen_steps": ["retrieve"]}).json()
    print(f"重开的步骤：{resumed.get('reopened')}")
    print(f"恢复后：action={resumed['action']} executed={resumed['executed']} "
          f"complete={resumed['complete']} state={resumed['state']}")
    print(f"检索工具总调用次数：{tool.calls}（对账后复用结果，没有再发）")
    db.close()


def scenario_partial_write(tmp):
    """3) 写入部分生效 → 自动补偿 → 世界复原。"""
    _title("场景 3 · 写入部分生效：自动补偿，不留残渣")
    writer = ControllableWriteTool(mode="partial")
    client, db = _client(os.path.join(tmp, "s3.db"),
                         tool=ScriptedResearchTool(), writer=writer)

    client.post("/tasks", json={"session_id": "w3", "goal": "写入订单"})
    payload = build_write_plan(target="order-3",
                               value={"amount": 100, "currency": "CNY"}).to_dict()
    for step in payload["steps"]:
        if step["step_id"] == "write":
            step["tool_args_template"] = {"target": "order-3",
                                          "value": {"amount": 100, "currency": "CNY"}}
        if step["step_id"] == "confirm":
            step["tool_args_template"] = {"target": "order-3"}
    client.post("/tasks/w3/plans", json={"plan": payload, "activate": True})
    body = client.post("/tasks/w3/plans/P1/advance", json={}).json()

    calls = GraphRepository(db).tool_calls("w3")
    print(f"步骤：{ {s['step_id']: s['status'] for s in body['steps']} }")
    print(f"工具调用：{[(c['status'], c['outcome']) for c in calls]}")
    print(f"补偿调用：{[(c['status'], bool(c['compensation_for'])) for c in calls]}")
    print(f"外部世界：{writer.world}（应为空——半成品被撤掉了）")
    print(f"任务终态：{body['state']}（写入确实没做成，所以是 failed）")
    db.close()


def scenario_crash_recovery(tmp):
    """4) 跑到一半崩溃 → 只补未完成的那几步。"""
    _title("场景 4 · 崩溃后恢复：只补未提交的那一步")
    tool = ScriptedResearchTool()
    client, db = _client(os.path.join(tmp, "s4.db"), tool=tool)
    repo = GraphRepository(db)

    client.post("/tasks", json={"session_id": "r4", "goal": PROBLEM})
    client.post("/tasks/r4/plans", json={"problem": PROBLEM, "activate": True})
    # 手工把前两步做完（模拟"跑到 draft 之后进程没了"）
    outputs = {
        "retrieve": {"refs": [{"source": "Albert Einstein", "chunk_id": "c1",
                               "quote": "1879 年出生于乌尔姆。"}], "answer": "1879 年"},
        "draft": {"draft": "爱因斯坦出生于 1879 年。"},
    }
    for step_id in ("retrieve", "draft"):
        statuses = repo.step_statuses("r4", "P1")
        if statuses.get(step_id) == "blocked":
            repo.set_step_status("r4", "P1", step_id, "pending", reason="依赖已满足")
        repo.claim_step("r4", "P1", step_id)
        repo.set_step_status("r4", "P1", step_id, "running")
        # 真实的 Attempt + output：下游 binding 要能从它取到输入
        attempt = repo.record_attempt("r4", "P1", step_id, status="running")
        repo.update_attempt("r4", "P1", attempt["attempt_id"], status="succeeded",
                            output=outputs[step_id])
        repo.set_step_status("r4", "P1", step_id, "verifying")
        repo.set_step_status("r4", "P1", step_id, "done")
    print(f"崩溃前：{repo.step_statuses('r4', 'P1')}")

    # 写一个 checkpoint：它会把"还没收敛的 Step"和"在途工具调用"一起记下来
    from atlas.service import ServiceRuntime

    ServiceRuntime(repo, "r4").checkpoint(pending_role="worker")
    checkpoint = client.get("/tasks/r4").json()["checkpoint"] or {}
    print(f"checkpoint 记下的待推进步骤：{checkpoint.get('pending_step_ids')}")
    print(f"checkpoint 记下的在途工具调用：{checkpoint.get('in_flight_tool_call_ids')}")

    # 新进程：换一个 repository 实例
    fresh = GraphRepository(db)
    recovered = client.post("/tasks/r4/plans/P1/recover", json={}).json()
    print(f"恢复执行：{recovered['executed']}（retrieve/draft 不该重跑）")
    print(f"恢复后状态：{fresh.step_statuses('r4', 'P1')}")
    db.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        scenario_research_and_write(tmp)
        scenario_unknown_then_recover(tmp)
        scenario_partial_write(tmp)
        scenario_crash_recovery(tmp)
    print("\n全部场景完成：计划 / 工具调用 / 补偿 / 恢复 四条链路都跑在真实代码路径上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
