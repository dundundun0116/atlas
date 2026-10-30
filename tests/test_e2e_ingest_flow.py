"""Task 5 的端到端闭环（进程内，不花 API 费用）。

链路：上传文档 → 构建版本 → 用**该版本**装载检索器 → 起 API → 提问 →
答案证据必须来自刚上传的文档，且运行记录里固定着同一个索引版本。
"""

import json

from fastapi.testclient import TestClient

from compassrag.agent import Agent, AgentConfig
from compassrag.api import IngestStore, create_app
from compassrag.llm.client import LLMResponse


class _StubLLM:
    model = "e2e-stub"
    embedding_model = ""

    def chat(self, messages, **kw):
        return LLMResponse(text="Paris", model="stub", prompt_tokens=50,
                           completion_tokens=2, total_tokens=52, latency_ms=1.0)


def test_ingest_to_answer_loop_pins_one_index_version(tmp_path):
    root = tmp_path / "ingest"
    store = IngestStore(root)
    store.put_document(__import__("compassrag.api.ingest", fromlist=["DocumentIn"]).DocumentIn(
        name="notes.md",
        content="# Notes\n\nThe capital of France is Paris. Lyon is another French city.",
        format="md"))
    version = store.build()
    assert version.n_chunks >= 1

    tools, chunks = store.load_tools(version.version)
    agent = Agent(_StubLLM(), tools, chunks, AgentConfig(top_k=3))
    client = TestClient(create_app(agent, ingest_root=root,
                                   runtime_index_version=version.version))

    # 状态：运行时版本 = 最新构建，不告警
    status = client.get("/index/status").json()
    assert status["runtime_version"] == version.version
    assert status["mismatch"] is False

    # 提问：证据必须来自上传的文档
    result = client.post("/research", json={"query": "What is the capital of France?",
                                            "max_rounds": 1}).json()
    assert result["answer"] == "Paris"
    titles = {e["source"] for e in result["evidence"]}
    assert "Notes" in titles, f"证据不是来自上传文档：{titles}"
    assert all(e["retriever"] == "bm25" for e in result["evidence"])

    # 运行记录固定索引版本（重建索引不影响已有运行）
    runs = client.get("/runs").json()
    assert runs and runs[0]["index_version"] == version.version
    trace_id = runs[0]["trace_id"]
    detail = client.get(f"/runs/{runs[0]['run_id']}").json()
    assert detail["trace"]["run_id"] == trace_id
    assert [s["kind"] for s in detail["trace"]["steps"]][:2] == ["retrieve", "answer"]


def test_rebuild_after_start_does_not_change_runtime_version(tmp_path):
    """重建只产生新版本：运行时版本仍是启动时那一版，状态会报 mismatch。"""
    root = tmp_path / "ingest"
    store = IngestStore(root)
    from compassrag.api.ingest import DocumentIn
    store.put_document(DocumentIn(name="a.txt", content="Paris is in France."))
    first = store.build()

    tools, chunks = store.load_tools(first.version)
    client = TestClient(create_app(Agent(_StubLLM(), tools, chunks, AgentConfig(top_k=2)),
                                   ingest_root=root,
                                   runtime_index_version=first.version))

    store.put_document(DocumentIn(name="b.txt", content="Lyon is also in France."))
    rebuilt = client.post("/index/build", json={}).json()
    assert rebuilt["version"] != first.version

    status = client.get("/index/status").json()
    assert status["runtime_version"] == first.version
    assert status["built_version"] == rebuilt["version"]
    assert status["mismatch"] is True
