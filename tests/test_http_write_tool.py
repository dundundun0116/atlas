"""真实写工具适配器：把 HTTP 服务接到 ToolCall 协议上（设计文档 §6 / §8 / §11）。

这里的服务**真的在 127.0.0.1 上监听**：请求走真 socket，超时是真超时，5xx 是真 5xx。
之所以不用 mock，是因为这套协议要解决的就是"网络把结果弄丢了"这件事——
把网络换成假的，测出来的只能是"我以为网络会这样"。

两条主线：

- **能自动收敛**（`IDEMPOTENT_PROFILE`）：写入超时 → `indeterminate` → 按幂等键查回原结果
  → `applied`，全程只写一次；
- **收敛不了就交给人**（`LEGACY_PROFILE`）：不认幂等键、没有查询端点 → 未知永远只能是
  `unknowable`，而且因为不可补偿，**派发之前**就必须过人工闸门。
"""

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from atlas.api import create_app
from atlas.planning.write_workflow import build_write_plan
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools.calls import ToolCallStatus
from atlas.tools.compensation import decide_after_call, requires_human_approval
from atlas.tools.dispatcher import ToolDispatcher
from atlas.tools.http_write import (
    IDEMPOTENT_PROFILE,
    LEGACY_PROFILE,
    HttpWriteError,
    HttpWriteTool,
    ServiceProfile,
    classify_status,
)

VALUE = {"amount": 100, "currency": "CNY"}


# ---------- 一个真的在跑的外部服务 ----------


class _World:
    """服务端状态：对象表、幂等键表、请求流水，以及一个可编程的故障剧本。"""

    def __init__(self):
        self.objects = {}
        self.idempotency = {}
        self.writes = []       # [(target, idempotency_key)]
        self.requests = []     # [(method, path, idempotency_key)]
        self.fault = None      # None / "500" / "partial" / "reject" / "sleep"
        self.delay = 0.8


def _handler_for(world: _World):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # 别把请求日志刷进测试输出
            return

        # -- 工具 --

        def _payload(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _send(self, code, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端已经超时走人了；这正是"写进去了但没人知道"的现场
                    pass

        @staticmethod
        def _last(path):
            segment = urllib.parse.urlparse(path).path.rstrip("/").rsplit("/", 1)[-1]
            return urllib.parse.unquote(segment)

        # -- 路由 --

        def do_PUT(self):
            target = self._last(self.path)
            key = self.headers.get("Idempotency-Key")
            value = self._payload()
            world.requests.append(("PUT", self.path, key))
            if world.fault == "reject":
                self._send(422, {"error": "字段不合法"})
                return
            if world.fault == "500":
                self._send(500, {"error": "内部错误"})
                return
            if world.fault == "partial":
                half = {name: value[name] for name in list(value)[:1]}
                world.objects[target] = half
                self._send(200, {"partial": True, "applied": half})
                return
            world.objects[target] = dict(value)
            if key:
                world.idempotency[key] = {"applied": dict(value)}
            world.writes.append((target, key))
            if world.fault == "sleep":
                # 关键：**先写进去，再拖到超时**——这才是真实的 unknown_commit
                time.sleep(world.delay)
            self._send(201, {"applied": dict(value)})

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            world.requests.append(("GET", path, None))
            if path.startswith("/idempotency/"):
                key = urllib.parse.unquote(path[len("/idempotency/"):])
                found = world.idempotency.get(key)
                if found is None:
                    self._send(404, {"error": "没有这个幂等键"})
                else:
                    self._send(200, found)
                return
            target = self._last(path)
            found = world.objects.get(target)
            if found is None:
                self._send(404, {"error": "对象不存在"})
            else:
                self._send(200, {"value": found})

        def do_DELETE(self):
            target = self._last(self.path)
            world.requests.append(("DELETE", target, None))
            removed = world.objects.pop(target, None)
            if removed is None:
                self._send(404, {"error": "对象不存在"})
            else:
                self._send(200, {"removed": removed})

    return Handler


@pytest.fixture()
def service():
    world = _World()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(world))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, world
    finally:
        server.shutdown()
        server.server_close()


def _tool(base_url, profile=IDEMPOTENT_PROFILE, timeout_s=None):
    if timeout_s is not None:
        profile = ServiceProfile(**{**profile.__dict__, "timeout_s": timeout_s})
    return HttpWriteTool(base_url=base_url, profile=profile)


@pytest.fixture()
def repo(tmp_path):
    database = open_database(tmp_path / "http_write.db")
    repository = GraphRepository(database)
    repository.create_session("s1", goal="写入")
    try:
        yield repository
    finally:
        database.close()


# ---------- 状态码分类（纯函数，先把它钉死） ----------


class TestClassifyStatus:
    @pytest.mark.parametrize("status", [200, 201, 202, 204])
    def test_2xx_is_success(self, status):
        assert classify_status(status, IDEMPOTENT_PROFILE) is None

    @pytest.mark.parametrize("status", [400, 403, 404, 422, 429])
    def test_4xx_is_determinate_rejection(self, status):
        # 服务端明确不受理 ⇒ 动作没发生，这是**确定结论**
        assert classify_status(status, IDEMPOTENT_PROFILE) == "invalid_request"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_unknown_not_failure(self, status):
        # 5xx 可能是"写完了才炸"，不能当失败重发
        assert classify_status(status, IDEMPOTENT_PROFILE) == "upstream_error"

    def test_408_is_unknown(self):
        # 超时是代理层的话：上游到底做没做没人知道
        assert classify_status(408, IDEMPOTENT_PROFILE) == "timeout"

    def test_unseen_status_defaults_to_unknown(self):
        assert classify_status(418, IDEMPOTENT_PROFILE) == "unknown_commit"

    def test_409_counts_as_applied_only_when_declared(self):
        # 没声明就不知道是"键重放"还是"对象已存在"——两件事后果完全不同
        assert classify_status(409, IDEMPOTENT_PROFILE) == "unknown_commit"
        replay = ServiceProfile(name="replay", treat_409_as_applied=True)
        assert classify_status(409, replay) is None


# ---------- 能力声明 ----------


class TestProfiles:
    def test_idempotent_profile_declares_all_three(self):
        spec = IDEMPOTENT_PROFILE.tool_spec()
        assert spec.supports_idempotency and spec.supports_lookup
        assert spec.supports_compensation
        assert requires_human_approval(spec) is False

    def test_legacy_profile_declares_nothing(self):
        spec = LEGACY_PROFILE.tool_spec()
        assert spec.supports_idempotency is False
        assert spec.supports_lookup is False
        assert spec.supports_compensation is False
        # 不可补偿的副作用：派发之前就要人点头
        assert requires_human_approval(spec) is True
        assert spec.retry_safe is False

    def test_spec_name_carries_profile(self, service):
        base_url, _ = service
        assert _tool(base_url).SPEC.name == "http_writer[idempotent_service]"


# ---------- 写入：五种真实后果 ----------


class TestWrite:
    def test_applied_writes_object_and_sends_idempotency_key(self, service):
        base_url, world = service
        tool = _tool(base_url)
        result = tool.write("order-1", VALUE, idempotency_key="k-1")
        assert result == {"applied": VALUE}
        assert world.objects["order-1"] == VALUE
        assert world.writes == [("order-1", "k-1")]

    def test_legacy_profile_sends_no_idempotency_header(self, service):
        base_url, world = service
        tool = _tool(base_url, LEGACY_PROFILE)
        tool.write("order-1", VALUE, idempotency_key="k-1")
        assert world.writes == [("order-1", None)]

    def test_rejection_is_determinate(self, service):
        base_url, world = service
        world.fault = "reject"
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url).write("order-1", VALUE, idempotency_key="k-1")
        assert excinfo.value.kind == "invalid_request"
        assert excinfo.value.status == 422
        assert world.objects == {}

    def test_5xx_is_unknown(self, service):
        base_url, world = service
        world.fault = "500"
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url, timeout_s=2.0).write("order-1", VALUE,
                                                 idempotency_key="k-1")
        assert excinfo.value.kind == "upstream_error"

    def test_timeout_is_unknown_but_world_changed(self, service):
        base_url, world = service
        world.fault = "sleep"
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url, timeout_s=0.2).write("order-1", VALUE,
                                                 idempotency_key="k-1")
        assert excinfo.value.kind == "timeout"
        # 服务端其实已经写了——这就是"不知道"的来源
        assert world.objects["order-1"] == VALUE

    def test_partial_is_reported_as_partial(self, service):
        base_url, world = service
        world.fault = "partial"
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url).write("order-1", VALUE, idempotency_key="k-1")
        assert excinfo.value.kind == "partial"
        assert world.objects["order-1"] == {"amount": 100}

    def test_connection_refused_is_unknown(self):
        # 连不上也要按"不知道"处理：请求可能发出去了，只是没人接
        tool = HttpWriteTool(base_url="http://127.0.0.1:1",
                             profile=ServiceProfile(name="dead", timeout_s=0.5))
        with pytest.raises(HttpWriteError) as excinfo:
            tool.write("order-1", VALUE, idempotency_key="k-1")
        assert excinfo.value.kind in ("unknown_commit", "timeout")


# ---------- 对账用的两个入口 + 补偿 ----------


class TestLookupObserveUndo:
    def test_lookup_finds_original_result(self, service):
        base_url, world = service
        tool = _tool(base_url)
        tool.write("order-1", VALUE, idempotency_key="k-1")
        assert tool.lookup("k-1") == {"applied": VALUE}

    def test_lookup_returns_none_when_absent(self, service):
        base_url, _ = service
        assert _tool(base_url).lookup("nope") is None

    def test_lookup_unsupported_raises_instead_of_lying(self, service):
        base_url, _ = service
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url, LEGACY_PROFILE).lookup("k-1")
        assert excinfo.value.kind == "unsupported"

    def test_observe_reads_object_back(self, service):
        base_url, _ = service
        tool = _tool(base_url)
        tool.write("order-1", VALUE, idempotency_key="k-1")
        assert tool.observe("order-1") == VALUE

    def test_observe_missing_returns_none(self, service):
        base_url, _ = service
        assert _tool(base_url).observe("ghost") is None

    def test_undo_removes_the_side_effect(self, service):
        base_url, world = service
        tool = _tool(base_url)
        tool.write("order-1", VALUE, idempotency_key="k-1")
        assert tool.undo("order-1")["compensated"] == "order-1"
        assert world.objects == {}
        with pytest.raises(HttpWriteError) as excinfo:
            tool.undo("order-1")
        assert excinfo.value.kind == "invalid_request"

    def test_undo_unsupported_for_legacy(self, service):
        base_url, _ = service
        with pytest.raises(HttpWriteError) as excinfo:
            _tool(base_url, LEGACY_PROFILE).undo("order-1")
        assert excinfo.value.kind == "unsupported"


# ---------- 接进 dispatcher：能不能自动收敛 ----------


class TestDispatcherReconcile:
    def _dispatcher(self, repo, base_url, profile=IDEMPOTENT_PROFILE):
        tool = _tool(base_url, profile, timeout_s=0.2)
        return tool, ToolDispatcher(repo, "s1", tool.SPEC)

    def test_timeout_then_lookup_settles_as_applied(self, service, repo):
        base_url, world = service
        world.fault = "sleep"
        tool, dispatcher = self._dispatcher(repo, base_url)
        outcome = dispatcher.invoke(
            lambda key: tool.write("order-1", VALUE, idempotency_key=key),
            {"target": "order-1", "value": VALUE},
        )
        assert outcome.status is ToolCallStatus.INDETERMINATE
        assert outcome.needs_human is True

        world.fault = None
        settled = dispatcher.reconcile(outcome.tool_call_id,
                                       lookup=tool.lookup,
                                       observe=lambda: tool.observe("order-1"))
        assert settled.status is ToolCallStatus.RECONCILED
        assert settled.ok is True
        assert settled.result == {"applied": VALUE}
        # 对账查回来的结果要落库，重跑那一步才能复用
        record = repo.get_tool_call("s1", outcome.tool_call_id)
        assert record["result"] == {"applied": VALUE}
        assert record["outcome"] == "applied"
        # 全程只写了一次
        assert len(world.writes) == 1

    def test_legacy_cannot_reconcile_by_key(self, service, repo):
        base_url, world = service
        world.fault = "500"
        tool, dispatcher = self._dispatcher(repo, base_url, LEGACY_PROFILE)
        outcome = dispatcher.invoke(
            lambda key: tool.write("order-1", VALUE, idempotency_key=key),
            {"target": "order-1", "value": VALUE},
        )
        assert outcome.status is ToolCallStatus.INDETERMINATE

        # 就算调用方传了 lookup，工具没声明这个能力就不能查——静默跳过等于骗人
        settled = dispatcher.reconcile(outcome.tool_call_id, lookup=tool.lookup)
        assert settled.status is ToolCallStatus.INDETERMINATE
        assert settled.needs_human is True
        record = repo.get_tool_call("s1", outcome.tool_call_id)
        assert record["outcome"] == "unknowable"
        assert record["error"]["evidence"]["capability"] == "lookup"
        # 提示词要把人往"人工"上引，而不是"重试"
        assert decide_after_call(record, tool.SPEC).action == "human"

    def test_lookup_failure_does_not_become_not_applied(self, service, repo):
        base_url, world = service
        world.fault = "500"
        tool, dispatcher = self._dispatcher(repo, base_url)
        outcome = dispatcher.invoke(
            lambda key: tool.write("order-1", VALUE, idempotency_key=key),
            {"target": "order-1", "value": VALUE},
        )
        world.fault = "500"  # 查询本身也炸

        def broken_lookup(_key):
            raise RuntimeError("查询端也挂了")

        settled = dispatcher.reconcile(outcome.tool_call_id, lookup=broken_lookup)
        assert settled.status is ToolCallStatus.INDETERMINATE
        record = repo.get_tool_call("s1", outcome.tool_call_id)
        assert record["outcome"] == "unknowable"
        assert record["error"]["evidence"]["stage"] == "lookup"

    def test_nothing_to_reconcile_with_is_unknowable(self, service, repo):
        base_url, world = service
        world.fault = "500"
        tool, dispatcher = self._dispatcher(repo, base_url)
        outcome = dispatcher.invoke(
            lambda key: tool.write("order-1", VALUE, idempotency_key=key),
            {"target": "order-1", "value": VALUE},
        )
        settled = dispatcher.reconcile(outcome.tool_call_id)
        assert settled.status is ToolCallStatus.INDETERMINATE
        assert repo.get_tool_call("s1", outcome.tool_call_id)["outcome"] == "unknowable"


# ---------- 接进 DAG：写进去、写不进去、以及"不许写" ----------


def _plan_payload(target="order-1", value=None, approved=None):
    payload = build_write_plan(target=target, value=value or VALUE).to_dict()
    for step in payload["steps"]:
        if step["step_id"] == "write":
            step["tool_args_template"] = {"target": target,
                                          "value": dict(value or VALUE)}
            if approved is not None:
                step["tool_args_template"]["approved"] = approved
        if step["step_id"] == "confirm":
            step["tool_args_template"] = {"target": target}
    return payload


def _client(db, tool):
    return TestClient(create_app(
        GraphRepository(db), write_tool=tool,
        orchestrate_llm=lambda prompt: "{}",
        context_token_counter=lambda text: (len(text) + 3) // 4,
    ))


def _run(client, target="order-1", approved=None):
    client.post("/tasks", json={"session_id": "s1", "goal": "写入"})
    submitted = client.post(
        "/tasks/s1/plans",
        json={"plan": _plan_payload(target, approved=approved), "activate": True}).json()
    plan_id = submitted["plan"]["plan_id"]
    body = client.post(f"/tasks/s1/plans/{plan_id}/advance", json={}).json()
    return plan_id, body


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "http_dag.db")
    try:
        yield database
    finally:
        database.close()


class TestWritePlanWithHttpTool:
    def test_ok_path_completes(self, service, db):
        base_url, world = service
        client = _client(db, _tool(base_url))
        _, body = _run(client)
        steps = {item["step_id"]: item["status"] for item in body["steps"]}
        assert steps == {"write": "done", "confirm": "done"}
        assert world.objects["order-1"] == VALUE

    def test_unknown_write_parks_the_step_instead_of_failing_it(self, service, db):
        base_url, world = service
        world.fault = "500"
        client = _client(db, _tool(base_url, timeout_s=2.0))
        _, body = _run(client)
        steps = {item["step_id"]: item["status"] for item in body["steps"]}
        # 未知 ≠ 失败：失败会让计划继续走，而外部可能已经写了
        assert steps["write"] == "needs_human"
        assert client.get("/tasks/s1").json()["state"] == "paused"
        assert len(world.writes) == 0
        calls = client.get("/tasks/s1/tool-calls").json()["tool_calls"]
        assert calls[0]["status"] == "indeterminate"
        assert calls[0]["outcome"] == "unknowable"

    def test_uncompensatable_tool_requires_approval_before_any_request(self, service, db):
        base_url, world = service
        client = _client(db, _tool(base_url, LEGACY_PROFILE))
        _, body = _run(client)
        steps = {item["step_id"]: item["status"] for item in body["steps"]}
        assert steps["write"] == "needs_human"
        # 关键断言：**一个请求都没发出去**
        assert world.requests == []
        assert client.get("/tasks/s1/tool-calls").json()["tool_calls"] == []

    def test_approved_legacy_tool_dispatches_but_stays_frozen_on_unknown(
            self, service, db):
        base_url, world = service
        world.fault = "500"
        client = _client(db, _tool(base_url, LEGACY_PROFILE, timeout_s=2.0))
        _, body = _run(client, approved=True)
        steps = {item["step_id"]: item["status"] for item in body["steps"]}
        assert steps["write"] == "needs_human"
        # 人工批准之后真的发出去了，而且只发了一次
        assert [item[0] for item in world.requests] == ["PUT"]
        calls = client.get("/tasks/s1/tool-calls").json()["tool_calls"]
        assert calls[0]["outcome"] == "unknowable"
