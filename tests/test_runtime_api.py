"""Task 1/4：公共运行时门面、暂停恢复、回放与事件损坏检测。

验收对应：
- 稳定 API：create / advance / pause / resume / state / trace / replay；
- 调用方只接触任务、状态、事件、结果契约（内部引擎/ORM 不外露）；
- 写请求幂等，重复请求返回原结果；
- 错误分类稳定（状态冲突 / 不存在 / 存储失败）；
- 跨进程重启后回归；事件回放能检测序列损坏。
"""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.runtime import (
    AgentRuntime,
    InvalidRequest,
    NotFound,
    StateConflict,
    StorageFailure,
)
from atlas.storage import events as storage_events
from atlas.storage.repository import GraphRepository, open_database


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "runtime.db")
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    return GraphRepository(db)


@pytest.fixture()
def runtime(repo):
    return AgentRuntime(repo)


class TestFacadeLifecycle:
    def test_create_task_returns_contract_fields(self, runtime):
        result = runtime.create_task("s1", "证明上界", request_id="req-1")
        assert result == {
            "session_id": "s1", "request_id": "req-1",
            "state": "planning", "replayed": False,
        }

    def test_create_task_is_idempotent(self, runtime, repo):
        first = runtime.create_task("s1", "g", idempotency_key="k1")
        second = runtime.create_task("s1", "g", idempotency_key="k1")
        assert second["replayed"] is True
        assert second["state"] == first["state"]
        assert repo.session_count() == 1

    def test_concurrent_same_key_only_advances_once(self, runtime, repo):
        runtime.create_task("s1", "g")

        def submit():
            return runtime.advance_task("s1", "plan_ready", idempotency_key="same")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        assert sorted(result["replayed"] for result in results) == [False, True]
        assert runtime.get_state("s1")["state"] == "executing"

    def test_advance_moves_state_and_checkpoints(self, runtime, repo):
        runtime.create_task("s1", "g")
        result = runtime.advance_task("s1", "plan_ready", reason="路线已定")
        assert result["state"] == "executing"
        assert repo.latest_checkpoint("s1").pending_role == "worker"

    def test_invalid_input_is_rejected_before_touching_storage(self, runtime):
        with pytest.raises(InvalidRequest):
            runtime.create_task("  ", "g")
        with pytest.raises(InvalidRequest):
            runtime.create_task("s1", "  ")

    def test_missing_session_is_not_found(self, runtime):
        with pytest.raises(NotFound):
            runtime.advance_task("ghost", "plan_ready")

    def test_illegal_transition_is_conflict_and_state_unchanged(self, runtime):
        runtime.create_task("s1", "g")
        with pytest.raises(StateConflict) as excinfo:
            runtime.advance_task("s1", "work_done")
        assert excinfo.value.status_code == 409
        assert runtime.get_state("s1")["state"] == "planning"

    def test_storage_failure_is_wrapped(self, repo):
        runtime = AgentRuntime(repo)
        repo.create_session = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full"))
        with pytest.raises(StorageFailure) as excinfo:
            runtime.create_task("s1", "g")
        assert excinfo.value.kind == "storage_failure"


class TestPauseResume:
    def test_pause_then_resume_returns_to_previous_state(self, runtime):
        runtime.create_task("s1", "g")
        runtime.advance_task("s1", "plan_ready")
        paused = runtime.pause_task("s1", reason="等预算")
        assert paused["state"] == "paused"
        resumed = runtime.resume_task("s1")
        assert resumed["state"] == "executing"

    def test_resume_without_checkpoint_is_conflict(self, runtime):
        runtime.create_task("s1", "g")
        with pytest.raises(StateConflict):
            runtime.resume_task("s1")

    def test_task_can_only_be_resumed_once(self, runtime):
        runtime.create_task("s1", "g")
        runtime.advance_task("s1", "plan_ready")
        runtime.pause_task("s1", reason="等预算")
        runtime.resume_task("s1", idempotency_key="r1")
        with pytest.raises(StateConflict):  # 已恢复过：不在暂停态
            runtime.resume_task("s1")
        replay = runtime.resume_task("s1", idempotency_key="r1")  # 同键重放返回原结果
        assert replay["replayed"] is True

    def test_pause_on_terminal_task_is_conflict(self, runtime):
        runtime.create_task("s1", "g")
        for trigger in ("plan_ready", "work_done", "audit_pass"):
            runtime.advance_task("s1", trigger)
        with pytest.raises(StateConflict):
            runtime.pause_task("s1")


class TestStateTraceReplay:
    def test_state_hides_internals_and_exposes_contract(self, runtime, repo):
        runtime.create_task("s1", "证明上界")
        runtime.advance_task("s1", "plan_ready")
        repo.add_node("s1", type_="fact", content="主引理", source="paper:1", confidence=0.9)
        state = runtime.get_state("s1")
        assert {"session_id", "goal", "state", "failure_reason", "nodes", "edges",
                "checkpoint", "provenance"} == set(state)
        assert state["nodes"][0]["source"] == "paper:1"
        # 证据/理由/meta 对 API 可见：统一工作台不读库也能展示依据
        assert {"reason", "evidence", "meta"} <= set(state["nodes"][0])
        assert state["failure_reason"] == ""
        assert state["checkpoint"]["pending_role"] == "worker"

    def test_trace_summary_counts_events(self, runtime):
        runtime.create_task("s1", "g")
        runtime.advance_task("s1", "plan_ready", reason="路线")
        trace = runtime.get_trace("s1")
        assert trace["summary"]["events"] == 1
        assert trace["events"][0]["agent_role"] == "worker"

    def test_report_lists_goal_and_artifacts(self, runtime, repo):
        runtime.create_task("s1", "证明上界")
        repo.add_node("s1", type_="artifact", content="报告草稿.md", verified_at="2026-10-28")
        report = runtime.get_report("s1")
        assert "证明上界" in report["report"]
        assert "报告草稿.md" in report["report"]

    def test_replay_returns_timeline_states_and_checkpoint(self, runtime, repo):
        runtime.create_task("s1", "g")
        runtime.advance_task("s1", "plan_ready")
        repo.add_node("s1", type_="decision", content="用鞅方法", reason="题设", actor="planner")
        payload = runtime.replay("s1")
        assert payload["ok"] is True
        # timeline 只含业务事件（trace 事件单列在 traces 里）
        assert [e["operation"] for e in payload["timeline"]] == ["add_node"]
        assert payload["traces"], "trace 事件应可在 replay 里查到"
        assert payload["states"]["D1"]["status"] == "active"
        assert payload["checkpoint"]["pending_role"] == "worker"
        json.dumps(payload)  # 契约必须可序列化


class TestEventCorruption:
    def _event(self, seq, operation, payload):
        return SimpleNamespace(
            seq=seq, operation=operation, payload=payload,
            event_id=f"e{seq}", actor="test",
        )

    def test_clean_sequence_has_no_problems(self):
        events = [
            self._event(1, "add_node", {"node_id": "D1"}),
            self._event(2, "add_node", {"node_id": "D2"}),
            self._event(3, "supersede", {"node_id": "D1", "superseded_by": "D2"}),
        ]
        states, problems = storage_events.replay_with_problems(events)
        assert problems == []
        assert states["D1"]["status"] == "superseded"
        assert states["D2"]["status"] == "active"

    def test_duplicate_seq_is_detected(self):
        events = [
            self._event(2, "add_node", {"node_id": "D1"}),
            self._event(2, "add_node", {"node_id": "D2"}),
        ]
        _, problems = storage_events.replay_with_problems(events)
        assert any("seq 非单调" in p for p in problems)

    def test_unknown_supersede_target_is_detected(self):
        events = [
            self._event(1, "add_node", {"node_id": "D1"}),
            self._event(2, "supersede", {"node_id": "D1", "superseded_by": "D9"}),
        ]
        _, problems = storage_events.replay_with_problems(events)
        assert any("supersede 指向未知节点" in p for p in problems)

    def test_terminal_node_change_is_detected(self):
        events = [
            self._event(1, "add_node", {"node_id": "D1"}),
            self._event(2, "add_node", {"node_id": "D2"}),
            self._event(3, "supersede", {"node_id": "D1", "superseded_by": "D2"}),
            self._event(4, "expire", {"node_id": "D1", "reason": "又过期"}),
        ]
        _, problems = storage_events.replay_with_problems(events)
        assert any("仍被标记过期" in p for p in problems)

    def test_unknown_edge_endpoint_is_detected(self):
        events = [
            self._event(1, "add_node", {"node_id": "D1"}),
            self._event(2, "link", {"src": "D1", "dst": "D9", "type": "supersedes"}),
        ]
        _, problems = storage_events.replay_with_problems(events)
        assert any("边引用未知节点" in p for p in problems)


class TestHttpNewEndpoints:
    @pytest.fixture()
    def client(self, db):
        return TestClient(create_app(GraphRepository(db)))

    def test_pause_and_resume_over_http(self, client):
        client.post("/tasks", json={"session_id": "s1", "goal": "g"})
        client.post("/tasks/s1/advance", json={"trigger": "plan_ready"})
        paused = client.post("/tasks/s1/pause", json={"reason": "等外部输入"})
        assert paused.json()["state"] == "paused"
        resumed = client.post("/tasks/s1/resume", json={"request_id": "req-r"})
        assert resumed.json()["state"] == "executing"
        assert resumed.json()["request_id"] == "req-r"

    def test_replay_endpoint_returns_contract(self, client):
        client.post("/tasks", json={"session_id": "s1", "goal": "g"})
        client.post("/tasks/s1/advance",
                    json={"trigger": "plan_ready", "idempotency_key": "k-adv"})
        body = client.get("/tasks/s1/replay").json()
        assert body["ok"] is True
        assert [e["operation"] for e in body["timeline"]] == ["advance_task"]
        assert body["graph_version"] == 0

    def test_errors_keep_request_id(self, client):
        response = client.post("/tasks/ghost/advance",
                               json={"trigger": "plan_ready", "request_id": "req-x"})
        assert response.status_code == 404
        assert response.json()["error"]["request_id"] == "req-x"
