"""审计引用校验收口（D10 债务）：图能给出真实节点 id 时，Auditor 不许引用不存在的节点。

两条路径都覆盖：
- repository 路径（`RepositoryGraph` + `ServiceRuntime`，合并后的主路径）；
- legacy/宽松路径：图客户端没有 `known_ids()` 时保持原行为，不被误收紧。
"""

import json

import pytest

from atlas.orchestration import OrchestrationError, Orchestrator, TaskStateMachine
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database
from integration.merged_e2e import RepositoryGraph

PLAN = json.dumps({"route": "先检索再作答", "risks": "", "reason": "事实型问题"})


def _audit(refs):
    return json.dumps({"verdict": "ok", "issues": "", "refs": refs})


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt):
        return self.replies.pop(0)


def _setup(tmp_path):
    database = open_database(tmp_path / "known.db")
    repository = GraphRepository(database)
    repository.create_session("s1", goal="回答问题")
    return database, repository


class LenientGraph:
    """没有 known_ids() 的旧图客户端：保持宽松模式（只校验非空）。"""

    def state_view(self, role):
        return "视图"

    def role_note(self, role, kind, content, reason="", evidence=""):
        return {"node_id": f"{role}-{kind}"}


def test_service_runtime_exposes_real_node_ids(tmp_path):
    database, repository = _setup(tmp_path)
    try:
        service = ServiceRuntime(repository, "s1")
        assert service.known_ids() == set()
        row = service.note("planner", "路线", kind="decision", reason="题设")
        assert service.known_ids() == {row.node_id}
    finally:
        database.close()


def test_auditor_reference_to_real_node_passes(tmp_path):
    database, repository = _setup(tmp_path)
    try:
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿", _audit(["D1"])]),
            RepositoryGraph(repository, "s1"),
            machine=TaskStateMachine(),
        )
        answer, _ = orchestrator.solve("回答问题", session_id="s1", subtask_id="S1")
        assert answer == "草稿"
    finally:
        database.close()


def test_auditor_reference_to_unknown_node_is_rejected(tmp_path):
    database, repository = _setup(tmp_path)
    try:
        orchestrator = Orchestrator(
            ScriptedLLM([PLAN, "草稿", _audit(["F999"])]),
            RepositoryGraph(repository, "s1"),
            machine=TaskStateMachine(),
        )
        with pytest.raises(OrchestrationError) as excinfo:
            orchestrator.solve("回答问题", session_id="s1", subtask_id="S1")
        assert "不存在" in str(excinfo.value)
    finally:
        database.close()


def test_graph_without_known_ids_keeps_legacy_lenient_mode():
    orchestrator = Orchestrator(
        ScriptedLLM([PLAN, "草稿", _audit(["任何引用"])]),
        LenientGraph(),
        machine=TaskStateMachine(),
    )
    answer, _ = orchestrator.solve("回答问题", session_id="s", subtask_id="S1")
    assert answer == "草稿"
