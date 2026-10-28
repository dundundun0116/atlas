"""阶段 E：任务 API 契约、幂等、错误分类、trace 与恢复。

对应计划阶段 E 的任务与验收：
- 创建 / 推进 / 查状态 / 恢复 checkpoint / 读 trace / 取报告；
- 每个请求带 session id、request id、幂等键；
- 统一错误：参数 / 状态冲突 / 模型超时 / 存储失败 / 不可恢复 / 未找到；
- trace 记录角色、工具、模型、token、延迟、重试、状态版本、停止原因；
- 验收：重复请求安全；失败可定位到角色/调用/事件；服务重启后能继续。
"""

import json

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.observability.tracing import Tracer, hash_input
from atlas.storage.repository import GraphRepository, open_database


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "api.db")
    yield database
    database.close()


@pytest.fixture()
def app(db):
    return create_app(GraphRepository(db))


@pytest.fixture()
def client(app):
    return TestClient(app)


def create_task(client, session_id="s1", goal="证明上界", key="k-create", request_id="req-1"):
    return client.post(
        "/tasks",
        json={"session_id": session_id, "goal": goal,
              "request_id": request_id, "idempotency_key": key},
    )


def advance(client, session_id="s1", trigger="plan_ready", key="", reason="路线已定"):
    body = {"trigger": trigger, "reason": reason}
    if key:
        body["idempotency_key"] = key
    return client.post(f"/tasks/{session_id}/advance", json=body)


class TestHealth:
    def test_health_reports_storage_and_sessions(self, client):
        body = client.get("/healthz").json()
        assert body["ok"] is True
        assert body["storage"] == "ok"
        assert body["sessions"] == 0
        assert body["schema_version"]


class TestCreateTask:
    def test_create_returns_state_and_echoes_request_id(self, client):
        response = create_task(client)
        assert response.status_code == 200
        body = response.json()
        assert (body["session_id"], body["state"]) == ("s1", "planning")
        assert body["request_id"] == "req-1"
        assert body["replayed"] is False

    def test_same_idempotency_key_replays_without_recreating(self, client, db):
        first = create_task(client).json()
        second = create_task(client).json()
        assert second["replayed"] is True
        assert second["state"] == first["state"]
        assert GraphRepository(db).session_count() == 1

    def test_empty_session_id_is_rejected_as_invalid(self, client):
        response = client.post("/tasks", json={"session_id": "  ", "goal": "g"})
        assert response.status_code == 422


class TestAdvance:
    def test_advance_moves_state_and_records_checkpoint(self, client, db):
        create_task(client)
        response = advance(client, key="k-adv-1")
        body = response.json()
        assert (body["state"], body["replayed"]) == ("executing", False)
        checkpoint = GraphRepository(db).latest_checkpoint("s1")
        assert checkpoint.pending_role == "worker"
        assert checkpoint.task_state == "executing"

    def test_illegal_transition_is_state_conflict(self, client):
        create_task(client)
        response = advance(client, trigger="work_done")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["type"] == "state_conflict"
        assert "work_done" in error["message"]

    def test_unknown_trigger_is_invalid_request(self, client):
        create_task(client)
        response = advance(client, trigger="teleport")
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request"

    def test_advance_on_missing_session_is_not_found(self, client):
        response = advance(client, session_id="ghost")
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "not_found"

    def test_repeated_advance_with_same_key_is_idempotent(self, client, db):
        create_task(client)
        first = advance(client, key="k-adv-1").json()
        second = advance(client, key="k-adv-1").json()
        assert (first["state"], second["state"]) == ("executing", "executing")
        assert second["replayed"] is True
        # 事件表里只有一条 advance 结果（重放不新增）
        repo = GraphRepository(db)
        results = [e for e in repo.events("s1") if e.operation == "advance_task"]
        assert len(results) == 1

    def test_storage_failure_maps_to_503(self, db):
        app = create_app(GraphRepository(db))
        client = TestClient(app)

        def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        db.session_factory  # 触发属性存在性
        repo = GraphRepository(db)
        repo.create_session = boom  # type: ignore[assignment]
        app2 = create_app(repo)
        response = TestClient(app2).post("/tasks", json={"session_id": "s1", "goal": "g"})
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "storage_failure"


class TestStateView:
    def test_state_view_lists_nodes_edges_and_checkpoint(self, client, db):
        create_task(client)
        advance(client)
        repo = GraphRepository(db)
        repo.add_node("s1", type_="fact", content="主引理", source="paper:1", confidence=0.9)
        body = client.get("/tasks/s1").json()
        assert body["state"] == "executing"
        assert body["goal"] == "证明上界"
        assert body["nodes"][0]["source"] == "paper:1"
        assert body["checkpoint"]["pending_role"] == "worker"
        assert body["provenance"]["stale"] == []


class TestTrace:
    def test_trace_records_role_state_version_and_stop_reason(self, client, db):
        create_task(client)
        advance(client, key="k-adv-1")
        body = client.get("/tasks/s1/trace").json()
        assert body["summary"]["events"] == 1
        event = body["events"][0]
        assert event["agent_role"] == "worker"
        assert event["step_id"] == "plan_ready"
        assert event["stop_reason"] == "路线已定"
        assert event["state_version"] == 0
        assert event["outcome"] == "ok"
        assert event["trace_id"].startswith("trace-")

    def test_tracer_summary_aggregates_tokens_and_errors(self):
        tracer = Tracer(run_id="run-1")
        tracer.record(agent_role="planner", prompt_tokens=100, completion_tokens=20,
                      latency_ms=12.5, outcome="ok")
        tracer.record(agent_role="worker", prompt_tokens=50, retries=2, outcome="error",
                      stop_reason="timeout", input_text="同一个输入")
        summary = tracer.summary()
        assert summary["events"] == 2
        assert summary["tokens"] == 170
        assert summary["errors"] == 1
        assert summary["retries"] == 2
        assert tracer.events[-1].input_hash == hash_input("同一个输入")


class TestReportAndRestore:
    def test_report_contains_goal_and_artifact(self, client, db):
        create_task(client)
        advance(client)
        repo = GraphRepository(db)
        repo.add_node("s1", type_="artifact", content="最终报告草稿.md",
                      source="worker", verified_at="2026-10-28")
        body = client.get("/tasks/s1/report").json()
        assert body["state"] == "executing"
        assert "证明上界" in body["report"]
        assert "最终报告草稿.md" in body["report"]

    def test_restore_returns_state_from_checkpoint(self, client):
        create_task(client)
        advance(client)
        response = client.post("/tasks/s1/restore")
        assert response.status_code == 200
        assert response.json()["state"] == "executing"

    def test_restore_without_checkpoint_is_conflict(self, client):
        create_task(client)
        response = client.post("/tasks/s1/restore")
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "state_conflict"


class TestRestart:
    def test_task_continues_after_service_restart(self, tmp_path):
        path = tmp_path / "restart.db"
        database = open_database(path)
        client = TestClient(create_app(GraphRepository(database)))
        create_task(client)
        advance(client, key="k-1")
        database.close()

        database2 = open_database(path)
        try:
            client2 = TestClient(create_app(GraphRepository(database2)))
            assert client2.get("/tasks/s1").json()["state"] == "executing"
            response = client2.post("/tasks/s1/advance",
                                    json={"trigger": "work_done", "reason": "草稿完成",
                                          "idempotency_key": "k-2"})
            assert response.json()["state"] == "auditing"
            # 幂等键在重启后依然有效
            replay = client2.post("/tasks/s1/advance",
                                  json={"trigger": "work_done", "reason": "草稿完成",
                                        "idempotency_key": "k-2"})
            assert replay.json()["replayed"] is True
        finally:
            database2.close()


class TestEngineMirror:
    """评测链路旁路镜像：内存引擎的图能幂等进库，API 查得到。"""

    def test_import_engine_preserves_ids_status_and_is_idempotent(self, db, client):
        from atlas.memory.engine import GraphMemoryEngine
        from atlas.memory.schema import EdgeType

        repo = GraphRepository(db)
        repo.create_session("s1", goal="g")
        engine = GraphMemoryEngine()
        engine.add_goal("g")
        s1 = engine.add_subtask("订票")
        d1 = engine.add_decision("住西湖区", reason="近")
        d2 = engine.add_decision("住滨江", reason="便宜")
        engine.link(s1, "G1", EdgeType.SUBTASK_OF)
        engine.supersede(d1, d2)
        engine.expire(s1, "需求变更")

        first = repo.import_engine("s1", engine)
        second = repo.import_engine("s1", engine)
        assert first == {"nodes": 4, "edges": 2}
        assert second == {"nodes": 0, "edges": 0}

        body = client.get("/tasks/s1").json()
        statuses = {n["id"]: n["status"] for n in body["nodes"]}
        assert statuses["D1"] == "superseded"
        assert statuses["S1"] == "expired"
        assert {e["type"] for e in body["edges"]} == {"subtask_of", "supersedes"}
        imported_events = GraphRepository(db).events("s1")
        assert [event.operation for event in imported_events].count("add_node") == 4
        assert [event.operation for event in imported_events].count("link") == 2
        replayed = GraphRepository(db).replay_states("s1")
        assert replayed["D1"]["status"] == "superseded"
        assert replayed["S1"]["status"] == "expired"


class TestMemoryServerSessionRouting:
    def test_role_notes_are_scoped_to_requested_user(self):
        from eval import atlas_memory_server as server
        from atlas.service import AtlasMemorySystem

        token_counter = lambda text: max(1, len(str(text).split()))
        previous = dict(server.SYSTEMS)
        try:
            server.SYSTEMS.clear()
            server.SYSTEMS["u1"] = AtlasMemorySystem(
                "u1", llm=lambda _prompt: "{}", token_counter=token_counter
            )
            server.SYSTEMS["u2"] = AtlasMemorySystem(
                "u2", llm=lambda _prompt: "{}", token_counter=token_counter
            )
            server.role_note(
                server.RoleNoteRequest(
                    user_id="u1", role="planner", kind="decision", content="u1 route", reason="r"
                )
            )
            assert "u1 route" in server.state_view(
                server.StateViewRequest(user_id="u1")
            )["view"]
            assert "u1 route" not in server.state_view(
                server.StateViewRequest(user_id="u2")
            )["view"]
        finally:
            server.SYSTEMS.clear()
            server.SYSTEMS.update(previous)
