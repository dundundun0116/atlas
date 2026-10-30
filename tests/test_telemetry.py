import json

from compassrag.llm.telemetry import Telemetry, summarize


def test_record_appends_parseable_row(tmp_path):
    tel = Telemetry(tmp_path / "t.jsonl")
    row = tel.record(kind="chat", tag="smoke", model="m1", prompt_tokens=10,
                     completion_tokens=5, total_tokens=15, latency_ms=123.4)
    lines = (tmp_path / "t.jsonl").read_text().splitlines()
    assert len(lines) == 1
    back = json.loads(lines[0])
    assert back == row
    assert back["kind"] == "chat" and back["tag"] == "smoke" and back["model"] == "m1"
    assert back["total_tokens"] == 15 and back["latency_ms"] == 123.4
    assert "ts" in back


def test_record_optional_fields(tmp_path):
    tel = Telemetry(tmp_path / "t.jsonl")
    tel.record(kind="embed", tag="e", model="m2", total_tokens=9, latency_ms=1.0, n_items=7)
    back = json.loads((tmp_path / "t.jsonl").read_text())
    assert back["n_items"] == 7
    assert "error" not in back


def test_record_error_row_truncated(tmp_path):
    tel = Telemetry(tmp_path / "t.jsonl")
    tel.record(kind="chat", tag="t", model="m", latency_ms=1.0, error="x" * 1000)
    back = json.loads((tmp_path / "t.jsonl").read_text())
    assert len(back["error"]) <= 300


def test_summarize_aggregates(tmp_path):
    tel = Telemetry(tmp_path / "t.jsonl")
    tel.record(kind="chat", tag="smoke", model="m1", prompt_tokens=10, completion_tokens=5, total_tokens=15, latency_ms=100.0)
    tel.record(kind="chat", tag="smoke", model="m1", prompt_tokens=20, completion_tokens=5, total_tokens=25, latency_ms=300.0)
    tel.record(kind="embed", tag="smoke", model="e1", total_tokens=9, latency_ms=50.0, n_items=2)
    out = summarize(tmp_path / "t.jsonl")
    by_key = {(r["kind"], r["tag"], r["model"]): r for r in out}
    chat = by_key[("chat", "smoke", "m1")]
    assert chat["calls"] == 2 and chat["prompt_tokens"] == 30 and chat["total_tokens"] == 40
    assert chat["avg_latency_ms"] == 200.0
    emb = by_key[("embed", "smoke", "e1")]
    assert emb["calls"] == 1 and emb["n_items_sum"] == 2


def test_summarize_missing_file_returns_empty(tmp_path):
    assert summarize(tmp_path / "nope.jsonl") == []
