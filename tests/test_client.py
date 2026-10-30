import json

import pytest

from compassrag.llm.client import LLMClient, LLMError, LLMResponse, batch_texts


def _make_client(tmp_path):
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_API_KEY=sk-test\n"
        "LLM_MODEL=test-model\n"
        "EMBEDDING_MODEL=test-embed\n"
    )
    return LLMClient(repo_root=tmp_path)


def test_batch_texts_sizes():
    assert batch_texts(list(range(250)), 96) == [list(range(96)), list(range(96, 192)), list(range(192, 250))]
    assert batch_texts(["a"], 8) == [["a"]]


def test_missing_env_raises_actionable_error(tmp_path, monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LLMError, match=r"\.env"):
        LLMClient(repo_root=tmp_path)


def test_init_reads_config_and_defaults(tmp_path, monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)  # 防跨测试的 dotenv 环境残留
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_API_KEY=sk-test\n"
        "LLM_MODEL=test-model\n"
    )
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "models.yaml").write_text(
        "request:\n  timeout_s: 5\n  max_retries: 1\n"
        "embedding:\n  batch_size: 7\n"
        "telemetry:\n  path: runs/t.jsonl\n"
    )
    client = LLMClient(repo_root=tmp_path)
    assert client.model == "test-model"
    assert client.embedding_model == ""  # 未显式配置不回退主模型（见 embed 报错）
    assert client._batch_size == 7
    assert client.telemetry.path == tmp_path / "runs" / "t.jsonl"


def test_client_sends_opencode_session_header_and_ua(tmp_path):
    client = _make_client(tmp_path)
    headers = client._client.default_headers
    assert client.session_id and headers.get("x-opencode-session") == client.session_id
    assert headers.get("User-Agent", "").startswith("CompassRAG")
    # 显式传入 session 时可固定；不传则每次实例不同
    fixed = LLMClient(repo_root=tmp_path, session_id="fixed-123")
    assert fixed.session_id == "fixed-123"


def test_embed_requires_explicit_model(tmp_path, monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "LLM_BASE_URL=https://example.invalid/v1\nLLM_API_KEY=sk\nLLM_MODEL=m\n")
    client = LLMClient(repo_root=tmp_path)
    with pytest.raises(LLMError, match="EMBEDDING_MODEL"):
        client.embed(["text"])


def test_llm_response_fields():
    r = LLMResponse(text="hi", model="m", prompt_tokens=1, completion_tokens=2, total_tokens=3, latency_ms=4.0)
    assert (r.text, r.model, r.total_tokens) == ("hi", "m", 3)
    assert r.finish_reason == "" and r.reasoning_tokens == 0


def _fake_chat_response(content="Paris", finish_reason="length", reasoning_tokens=7):
    from types import SimpleNamespace as NS
    details = NS(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    return NS(choices=[NS(message=NS(content=content, reasoning_content="thinking"),
                          finish_reason=finish_reason)],
              usage=NS(prompt_tokens=11, completion_tokens=30, total_tokens=41,
                       completion_tokens_details=details))


def test_chat_captures_finish_reason_and_reasoning_tokens(tmp_path):
    client = _make_client(tmp_path)
    client._client.chat.completions.create = lambda **kw: _fake_chat_response()
    r = client.chat([{"role": "user", "content": "hi"}], tag="answer", max_tokens=32)
    assert r.finish_reason == "length" and r.reasoning_tokens == 7
    rows = [json.loads(l) for l in (tmp_path / "runs" / "telemetry.jsonl").read_text().splitlines()]
    assert rows[-1]["finish_reason"] == "length" and rows[-1]["reasoning_tokens"] == 7


def test_chat_without_reasoning_details_still_records(tmp_path):
    client = _make_client(tmp_path)
    client._client.chat.completions.create = lambda **kw: _fake_chat_response(
        content="Paris", finish_reason="stop", reasoning_tokens=None)
    r = client.chat([{"role": "user", "content": "hi"}], tag="answer")
    assert r.finish_reason == "stop" and r.reasoning_tokens == 0
    rows = [json.loads(l) for l in (tmp_path / "runs" / "telemetry.jsonl").read_text().splitlines()]
    assert rows[-1]["finish_reason"] == "stop" and "reasoning_tokens" not in rows[-1]


def test_chat_failure_recorded_then_raised(tmp_path):
    client = _make_client(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("boom: quota exhausted")

    client._client.chat.completions.create = boom
    with pytest.raises(RuntimeError, match="boom"):
        client.chat([{"role": "user", "content": "hi"}], tag="smoke")
    rows = [json.loads(l) for l in (tmp_path / "runs" / "telemetry.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "chat" and rows[-1]["error"].startswith("boom")


def test_embed_failure_recorded_then_raised(tmp_path):
    client = _make_client(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("boom: endpoint down")

    client._client.embeddings.create = boom
    with pytest.raises(RuntimeError, match="boom"):
        client.embed(["a", "b"], tag="smoke")
    rows = [json.loads(l) for l in (tmp_path / "runs" / "telemetry.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "embed" and rows[-1]["error"].startswith("boom")
