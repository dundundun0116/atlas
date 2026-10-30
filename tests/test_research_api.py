"""阶段 E：ResearchTool HTTP 接口（无真实 API，全部假 LLM）。

验收：结构化成功响应、五类失败可分支、幂等键重复请求返回同一 trace、
健康检查、trace 可按 id 回读、上游异常不 500、超时可控。
"""

import time

from fastapi.testclient import TestClient

from compassrag.agent import Agent, AgentConfig
from compassrag.api import create_app
from compassrag.llm.client import LLMResponse


class FakeTool:
    def __init__(self, ranking=("c1", "c2")):
        self.ranking = list(ranking)
        self.chunk_ids = list(ranking)   # 真索引也暴露这个属性，health 用它算版本

    def search(self, query, k=10):
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class BrokenTool:
    def search(self, query, k=10):
        raise RuntimeError("index exploded")


class SlowTool:
    def search(self, query, k=10):
        time.sleep(0.3)
        return [("c1", 1.0)]


class ScriptLLM:
    def __init__(self, text="Paris", script=None):
        self.text = text
        self.queue = list(script or [])
        self.calls = 0
        self.model = "fake-model"
        self.embedding_model = ""

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls += 1
        text = self.queue.pop(0) if self.queue else self.text
        return LLMResponse(text=text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0, finish_reason="stop")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
}


def _client(tools=None, *, trace_dir=None, **cfg):
    agent = Agent(ScriptLLM(), tools if tools is not None else {"bm25": FakeTool()},
                  STORE, AgentConfig(top_k=2, **cfg))
    return TestClient(create_app(agent, trace_dir=trace_dir)), agent


def test_health_reports_tools_and_model():
    client, _ = _client()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["tools"] == ["bm25"]
    assert body["model"] == "fake-model" and body["n_traces"] == 0


def test_research_returns_structured_result_and_trace_id():
    client, _ = _client()
    r = client.post("/research", json={"query": "Who is Alpha?", "max_rounds": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Paris"
    assert body["evidence"] and body["evidence"][0]["source"] == "Alpha"
    assert body["evidence"][0]["retriever"] == "bm25" and body["evidence"][0]["rank"] == 1
    assert body["stop_reason"] == "answered" and body["failure"] == ""
    assert body["trace_id"] and 0 < body["confidence"] <= 1


def test_trace_can_be_read_back_by_id(tmp_path):
    client, _ = _client(trace_dir=tmp_path)
    trace_id = client.post("/research", json={"query": "q"}).json()["trace_id"]
    r = client.get(f"/runs/{trace_id}")
    assert r.status_code == 200 and r.json()["found"] is True
    trace = r.json()["trace"]
    assert trace["run_id"] == trace_id and trace["answer"] == "Paris"
    assert trace["steps"] and trace["steps"][0]["kind"] == "retrieve"
    # 落盘版也能回读（新 client 内存为空）
    fresh, _ = _client(trace_dir=tmp_path)
    assert fresh.get(f"/runs/{trace_id}").json()["found"] is True


def test_unknown_trace_is_404():
    client, _ = _client()
    assert client.get("/runs/nope").status_code == 404


def test_idempotency_key_prevents_second_model_call():
    client, agent = _client()
    body = {"query": "q", "idempotency_key": "S3-attempt-1"}
    first = client.post("/research", json=body).json()
    second = client.post("/research", json=body).json()
    assert first["trace_id"] == second["trace_id"]
    assert agent.llm.calls == 1


def test_empty_query_is_422():
    client, _ = _client()
    assert client.post("/research", json={"query": "   "}).status_code == 422


def test_broken_tool_returns_upstream_error_not_500():
    client, _ = _client(tools={"bm25": BrokenTool()})
    r = client.post("/research", json={"query": "q"})
    assert r.status_code == 200
    assert r.json()["failure"] == "upstream_error"


def test_timeout_is_reported_structurally():
    client, _ = _client(tools={"bm25": SlowTool()})
    r = client.post("/research", json={"query": "q", "timeout_s": 0.05})
    assert r.status_code == 200
    assert r.json()["failure"] == "timeout"


def test_sync_timeout_marks_run_failed(tmp_path):
    client, _ = _client(tools={"bm25": SlowTool()}, trace_dir=tmp_path)
    r = client.post("/research/sync", json={"query": "q", "timeout_s": 0.01})
    assert r.json()["failure"] == "timeout"
    runs = client.get("/runs").json()
    assert runs[0]["status"] == "failed"
    assert runs[0]["failure"] == "timeout"


def test_budget_exhausted_is_branchable():
    import json as _json
    llm = ScriptLLM(script=[
        _json.dumps({"route": "multi", "subquestions": ["a?", "b?"]}),
        _json.dumps({"sufficient": False, "next_query": "c?"}),
    ])
    agent = Agent(llm, {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2, use_decision=True))
    client = TestClient(create_app(agent))
    body = client.post("/research", json={"query": "q", "budget_tokens": 20}).json()
    assert body["failure"] == "budget_exhausted"
    assert body["stop_reason"].startswith("budget_exhausted:")


# --------------------------------------------------------------------------- #
# Atlas adapter：本地与 HTTP 两种实现必须给出同一份契约
# --------------------------------------------------------------------------- #


def test_local_adapter_returns_contract_and_trace():
    from compassrag.adapters import LocalResearchTool
    from compassrag.agent import ResearchRequest

    agent = Agent(ScriptLLM(), {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2))
    tool = LocalResearchTool(agent)
    out = tool.research(ResearchRequest(query="Who is Alpha?"))
    assert out.answer == "Paris" and out.evidence[0].source == "Alpha"
    assert out.failure == "" and out.trace_id
    assert tool.get_trace(out.trace_id)["answer"] == "Paris"
    assert tool.get_trace("missing") is None


def test_http_adapter_speaks_the_same_contract():
    from compassrag.adapters import HTTPResearchTool
    from compassrag.agent import ResearchRequest

    agent = Agent(ScriptLLM(), {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2))
    client = TestClient(create_app(agent))
    tool = HTTPResearchTool(client=client)
    out = tool.research(ResearchRequest(query="Who is Alpha?"))
    assert out.answer == "Paris" and out.evidence[0].retriever == "bm25"
    assert tool.get_trace(out.trace_id)["run_id"] == out.trace_id
    assert tool.get_trace("missing") is None
    assert tool.health()["status"] == "ok"


def test_adapters_share_the_protocol():
    from compassrag.adapters import HTTPResearchTool, LocalResearchTool, ResearchTool

    agent = Agent(ScriptLLM(), {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2))
    client = TestClient(create_app(agent))
    assert isinstance(LocalResearchTool(agent), ResearchTool)
    assert isinstance(HTTPResearchTool(client=client), ResearchTool)


def test_http_adapter_maps_422_to_invalid_request():
    from compassrag.adapters import HTTPResearchTool
    from compassrag.agent import ResearchRequest

    agent = Agent(ScriptLLM(), {"bm25": FakeTool()}, STORE, AgentConfig(top_k=2))
    tool = HTTPResearchTool(client=TestClient(create_app(agent)))
    # 绕过 pydantic 直接发一个空 query，验证服务端 422 被翻译成契约里的失败类别
    out = tool.research(ResearchRequest(query="ok"))
    assert out.failure == ""
    resp = tool.client.post("/research", json={"query": "  "})
    assert resp.status_code == 422


def test_health_reports_index_version_and_size():
    client, _ = _client()
    body = client.get("/health").json()
    assert body["index"]["bm25"]["ok"] is True
    assert body["index"]["bm25"]["size"] == 2
    assert ":" in body["index"]["bm25"]["version"]


# --------------------------------------------------------------------------- #
# Task 2：异步运行、SSE 事件流、取消
# --------------------------------------------------------------------------- #


def _wait_terminal(client, run_id, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["summary"] and body["summary"]["status"] in ("completed", "failed", "cancelled"):
            return body["summary"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 没有在 {timeout}s 内进入终态")


def test_async_run_reaches_completed_with_summary_and_trace():
    client, _ = _client()
    submitted = client.post("/research", json={"query": "Who is Alpha?", "async_mode": True})
    assert submitted.status_code == 200
    run_id = submitted.json()["run_id"]
    assert submitted.json()["status"] in ("queued", "running")

    summary = _wait_terminal(client, run_id)
    assert summary["status"] == "completed"
    assert summary["answer"] == "Paris" and summary["stop_reason"] == "answered"
    assert summary["trace_id"] and summary["n_events"] > 0

    detail = client.get(f"/runs/{run_id}").json()
    assert detail["summary"]["run_id"] == run_id
    assert detail["trace"]["run_id"] == summary["trace_id"]


def test_sse_stream_emits_ordered_events_and_ends():
    client, _ = _client()
    run_id = client.post("/research", json={"query": "Who is Alpha?", "async_mode": True}).json()["run_id"]
    _wait_terminal(client, run_id)

    kinds = []
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("event:"):
                kinds.append(line.split(":", 1)[1].strip())
    assert "status" in kinds
    for expected in ("retrieve", "answer", "finish", "end"):
        assert expected in kinds, f"缺少事件 {expected}: {kinds}"
    # 顺序：retrieve 在 answer 之前，answer 在 finish 之前
    assert kinds.index("retrieve") < kinds.index("answer") < kinds.index("finish")


def test_cancel_marks_run_cancelled_and_stops_new_calls():
    import time
    slow = SlowTool()                 # 每次检索 sleep 0.3s
    client, agent = _client(tools={"bm25": slow})
    run_id = client.post("/research", json={"query": "q", "async_mode": True}).json()["run_id"]
    time.sleep(0.05)
    resp = client.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200 and resp.json()["cancelled"] is True
    summary = _wait_terminal(client, run_id, timeout=15)
    assert summary["status"] == "cancelled"
    assert summary["cancelled"] is True
    # 取消后不再发起新的模型调用
    assert agent.llm.calls <= 2


def test_list_runs_and_unknown_run_404():
    client, _ = _client()
    run_id = client.post("/research", json={"query": "q", "async_mode": True}).json()["run_id"]
    _wait_terminal(client, run_id)
    listed = client.get("/runs?limit=10").json()
    assert any(r["run_id"] == run_id for r in listed)
    assert client.get("/runs/nope/events").status_code == 404
    assert client.post("/runs/nope/cancel").status_code == 404


# --------------------------------------------------------------------------- #
# Task 1：会话分组与事件字段完整度
# --------------------------------------------------------------------------- #


def test_session_groups_runs_and_404_for_unknown():
    client, _ = _client()
    run_id = client.post("/research", json={"query": "q", "async_mode": True,
                                            "session_id": "s-1", "subtask_id": "S3"}).json()["run_id"]
    _wait_terminal(client, run_id)
    session = client.get("/sessions/s-1").json()
    assert session["session_id"] == "s-1"
    assert [r["run_id"] for r in session["runs"]] == [run_id]
    assert session["runs"][0]["subtask_id"] == "S3"
    assert client.get("/sessions/nope").status_code == 404


def test_events_carry_latency_tool_and_outcome():
    client, _ = _client()
    run_id = client.post("/research", json={"query": "q", "async_mode": True}).json()["run_id"]
    _wait_terminal(client, run_id)
    events = []
    with client.stream("GET", f"/runs/{run_id}/events") as resp:
        for line in resp.iter_lines():
            if line.startswith("data:"):
                import json as _json
                events.append(_json.loads(line.split(":", 1)[1]))
    retrieve = [e for e in events if e["kind"] == "retrieve"]
    assert retrieve, "没有检索事件"
    payload = retrieve[0]["payload"]
    for key in ("step_id", "latency_ms", "outcome", "tokens", "evidence"):
        assert key in payload, f"事件缺字段 {key}: {payload.keys()}"
    assert payload["tool"] == "bm25"
    assert payload["outcome"] == "ok"


def test_async_timeout_is_reported_as_failed_timeout():
    import time
    client, _ = _client(tools={"bm25": SlowTool()})
    run_id = client.post("/research", json={"query": "q", "async_mode": True,
                                            "timeout_s": 0.1}).json()["run_id"]
    time.sleep(0.2)
    summary = _wait_terminal(client, run_id, timeout=15)
    assert summary["status"] == "failed"
    assert summary["failure"] == "timeout"
    assert "timeout" in (summary["error"] or "")


def test_research_async_endpoint_returns_202_and_queued_summary():
    client, _ = _client()
    r = client.post("/research/async", json={"query": "q", "session_id": "s-9"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["run_id"]
    summary = _wait_terminal(client, body["run_id"])
    assert summary["status"] == "completed"
