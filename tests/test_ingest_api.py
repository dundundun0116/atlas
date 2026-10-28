"""Task 4：文档上传、索引版本、构建阶段与失败路径。"""

import json

import pytest
from fastapi.testclient import TestClient

from compassrag.agent import Agent, AgentConfig
from compassrag.api import create_app
from compassrag.api.ingest import IngestStore
from compassrag.llm.client import LLMResponse


class _Tool:
    chunk_ids = ["c1"]

    def search(self, query, k=10):
        return [("c1", 1.0)]


class _LLM:
    model = "fake"
    embedding_model = ""

    def chat(self, messages, **kw):
        return LLMResponse(text="Paris", model="fake", prompt_tokens=1,
                           completion_tokens=1, total_tokens=2, latency_ms=1.0)


def _client(tmp_path, runtime_version=""):
    agent = Agent(_LLM(), {"bm25": _Tool()}, {"c1": {"chunk_id": "c1", "title": "A", "text": "a"}},
                  AgentConfig(top_k=1))
    app = create_app(agent, ingest_root=tmp_path / "ingest",
                     runtime_index_version=runtime_version)
    return TestClient(app)


def test_upload_text_and_duplicate(tmp_path):
    c = _client(tmp_path)
    r = c.post("/documents", json={"name": "a.txt", "content": "hello world", "format": "txt"})
    assert r.status_code == 201
    body = r.json()
    assert body["duplicate"] is False and body["meta"]["doc_id"]
    again = c.post("/documents", json={"name": "a.txt", "content": "hello world", "format": "txt"})
    assert again.status_code == 201
    assert again.json()["duplicate"] is True
    assert again.json()["meta"]["doc_id"] == body["meta"]["doc_id"]
    assert len(c.get("/documents").json()) == 1


def test_upload_rejects_empty_bad_format_and_bad_json(tmp_path):
    c = _client(tmp_path)
    assert c.post("/documents", json={"name": "a", "content": "   "}).status_code == 422
    assert c.post("/documents", json={"name": "a", "content": "x", "format": "pdf"}).status_code == 422
    assert c.post("/documents", json={"name": "a", "content": "{not json",
                                      "format": "json"}).status_code == 422
    assert c.post("/documents", json={"name": "a", "content": '{"no_text": 1}',
                                      "format": "json"}).status_code == 422


def test_build_without_documents_returns_409(tmp_path):
    c = _client(tmp_path)
    r = c.post("/index/build")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_documents"


def test_build_creates_version_with_stages_and_files(tmp_path):
    c = _client(tmp_path)
    c.post("/documents", json={"name": "doc.md",
                               "content": "# Title\n\nSome text about Paris and Lyon.",
                               "format": "md"})
    c.post("/documents", json={"name": "docs.json",
                               "content": json.dumps([{"title": "Second", "text": "More text."}]),
                               "format": "json"})
    version = c.post("/index/build", json={}).json()
    assert version["n_documents"] == 2 and version["n_chunks"] >= 2
    stages = {s["name"]: s["status"] for s in version["stages"]}
    assert stages["chunk"] == "ok" and stages["bm25"] == "ok"
    assert stages["dense"] == "skipped" and stages["entity_graph"] == "skipped"

    root = tmp_path / "ingest" / "versions" / version["version"]
    assert (root / "chunks.jsonl").exists() and (root / "version.json").exists()
    assert (root / "bm25").exists()


def test_status_reports_runtime_version_mismatch(tmp_path):
    c = _client(tmp_path, runtime_version="v-start")
    c.post("/documents", json={"name": "a", "content": "hello"})
    version = c.post("/index/build", json={}).json()["version"]

    status = c.get("/index/status").json()
    assert status["runtime_version"] == "v-start"
    assert status["built_version"] == version
    assert status["mismatch"] is True
    assert status["n_documents"] == 1 and status["n_chunks"] > 0
    assert len(c.get("/index/versions").json()) >= 1


def test_build_failure_is_reported_not_raised(tmp_path):
    store = IngestStore(tmp_path / "ingest")
    # 直接写一个坏文档（模拟磁盘损坏/手工污染），构建应记录失败而不是 500
    (store.docs_dir / "bad.json").write_text(json.dumps({
        "meta": {"doc_id": "bad", "name": "bad", "format": "json",
                 "fingerprint": "f", "size": 1, "created_at": 0.0},
        "content": "{broken",
    }), encoding="utf-8")
    result = store.build()
    assert result.error == "chunk_failed"
    assert result.stages[0].status == "failed"
