"""多智能体编排测试（决策 #15–#17）：固定三段式、协调经图、审计打回与降级。

LLM 与图客户端均用替身注入，离线可跑。
"""

import json

import pytest

from atlas.memory.schema import NodeStatus
from atlas.orchestration import OrchestrationError, Orchestrator
from atlas.service import AtlasMemorySystem


def count_words(text: str) -> int:
    return max(1, len(text.split()))


class FakeGraph:
    def __init__(self, views=None):
        self.notes = []
        self.view_calls = []
        self.views = views or {}

    def state_view(self, role):
        self.view_calls.append(role)
        return self.views.get(role, f"（{role} 的状态视图）")

    def role_note(self, role, kind, content, reason="", evidence=""):
        self.notes.append(
            {"role": role, "kind": kind, "content": content, "reason": reason, "evidence": evidence}
        )
        return {"node_id": f"{role}-{kind}", "kind": kind, "actor": role}


class ScriptedLLM:
    """按调用顺序返回脚本化输出；元素为 Exception 时抛出（测降级）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


PLAN = json.dumps({"route": "用 Lemma 2 的鞅版本", "risks": "变量可能不独立", "reason": "题设依赖"})
AUDIT_OK = json.dumps({"verdict": "ok", "issues": ""})
AUDIT_REVISE = json.dumps({"verdict": "revise", "issues": "常数项用了 τσ²，应为 τ²σ²"})
PROBLEM = "题目：给出 L(h^t,w^t) 的上界"


class TestHappyPath:
    def test_ok_path_three_calls_and_notes(self):
        graph, llm = FakeGraph(), ScriptedLLM([PLAN, "草稿解答", AUDIT_OK])
        answer, trace = Orchestrator(llm, graph).solve(PROBLEM)
        assert answer == "草稿解答"
        assert trace.calls == ["planner", "worker", "auditor"]
        assert [(n["role"], n["kind"]) for n in graph.notes] == [
            ("planner", "decision"),
            ("worker", "artifact"),
        ]
        assert graph.view_calls == ["planner", "worker", "auditor"]  # 每角色都读图

    def test_revise_path_supersedes_draft(self):
        graph, llm = FakeGraph(), ScriptedLLM([PLAN, "草稿", AUDIT_REVISE, "修订后答案"])
        answer, trace = Orchestrator(llm, graph).solve(PROBLEM)
        assert answer == "修订后答案"
        assert trace.calls == ["planner", "worker", "auditor", "worker_revise"]
        assert [(n["role"], n["kind"]) for n in graph.notes] == [
            ("planner", "decision"),
            ("worker", "artifact"),
            ("auditor", "fact"),
            ("worker", "artifact_revise"),
        ]
        assert graph.notes[-1]["reason"] == "常数项用了 τσ²，应为 τ²σ²"

    def test_prompts_carry_state_view_and_route(self):
        graph = FakeGraph(views={"planner": "约束：γL≤1/24"})
        llm = ScriptedLLM([PLAN, "草稿", AUDIT_OK])
        Orchestrator(llm, graph).solve(PROBLEM)
        assert "约束：γL≤1/24" in llm.prompts[0] and PROBLEM in llm.prompts[0]
        assert "用 Lemma 2 的鞅版本" in llm.prompts[1]  # Worker 拿到 Planner 的路线

    def test_json_roles_use_no_think_llm_worker_uses_thinking(self):
        """Planner/Auditor 走关推理通道；Worker/修订走正常（推理）通道。"""
        graph = FakeGraph()
        think = ScriptedLLM(["草稿解答", "修订答案"])
        no_think = ScriptedLLM([PLAN, AUDIT_REVISE])
        answer, trace = Orchestrator(think, graph, llm_no_think=no_think).solve(PROBLEM)
        assert answer == "修订答案"
        assert len(no_think.prompts) == 2 and len(think.prompts) == 2
        assert "PLANNER" in no_think.prompts[0] and "AUDITOR" in no_think.prompts[1]
        assert "WORKER" in think.prompts[0] and "revising" in think.prompts[1]


class TestDegradation:
    def test_planner_bad_json_raises(self):
        graph, llm = FakeGraph(), ScriptedLLM(["不是 JSON"])
        with pytest.raises(OrchestrationError):
            Orchestrator(llm, graph).solve(PROBLEM)

    def test_llm_exception_raises_orchestration_error(self):
        graph, llm = FakeGraph(), ScriptedLLM([RuntimeError("boom")])
        with pytest.raises(OrchestrationError):
            Orchestrator(llm, graph).solve(PROBLEM)

    def test_empty_route_raises(self):
        graph, llm = FakeGraph(), ScriptedLLM([json.dumps({"route": "", "reason": "r"})])
        with pytest.raises(OrchestrationError):
            Orchestrator(llm, graph).solve(PROBLEM)

    def test_revision_failure_returns_draft(self):
        graph = FakeGraph()
        llm = ScriptedLLM([PLAN, "草稿", AUDIT_REVISE, RuntimeError("boom")])
        answer, trace = Orchestrator(llm, graph).solve(PROBLEM)
        assert answer == "草稿"  # 修订失败 → 保留草稿（auditor 疑虑已在图里）
        assert "worker_revise" in trace.calls


class TestRoleGraphInterface:
    """AtlasMemorySystem 的角色图接口（服务端 state_view / role_note 的数据源）。"""

    def make_system(self, **kwargs):
        return AtlasMemorySystem(
            "u1", llm=lambda p: "{}", token_counter=count_words, budget_tokens=4000, **kwargs
        )

    def test_state_view_contains_plan_and_hints(self):
        system = self.make_system()
        system.wrap_user_prompt("### PROBLEM:\n第一题")
        system.role_note("worker", "artifact", "草稿")
        system.role_note("worker", "artifact_revise", "修订", reason="常数项错")
        view = system.state_view("worker")
        assert "## 当前计划与约束" in view and "[S1]" in view
        assert "已作废" in view and "常数项错" in view  # 踩坑提示进视图

    def test_role_note_writes_nodes_with_actor(self):
        system = self.make_system()
        system.wrap_user_prompt("### PROBLEM:\n第一题")
        note = system.role_note("planner", "decision", "用引理 2", reason="题设依赖", evidence="变量独立")
        node = system.engine.nodes[note["node_id"]]
        assert (node.actor, node.reason, node.evidence) == ("planner", "题设依赖", "变量独立")
        system.role_note("auditor", "fact", "草稿问题：常数项错", reason="auditor 核验")
        actors = {n.actor for n in system.engine.nodes.values()}
        assert {"planner", "auditor"} <= actors

    def test_artifact_revise_supersedes_draft(self):
        system = self.make_system()
        system.wrap_user_prompt("### PROBLEM:\n第一题")
        first = system.role_note("worker", "artifact", "草稿")["node_id"]
        second = system.role_note("worker", "artifact_revise", "修订", reason="常数项错")["node_id"]
        assert system.engine.nodes[first].status == NodeStatus.SUPERSEDED
        assert system.engine.nodes[first].superseded_by == second
        assert "常数项错" in system.engine.nodes[second].note

    def test_role_note_validates(self):
        system = self.make_system()
        with pytest.raises(ValueError):
            system.role_note("planner", "decision", "无原因")
        with pytest.raises(ValueError):
            system.role_note("worker", "artifact", "   ")
        with pytest.raises(ValueError):
            system.role_note("worker", "unknown_kind", "x")
