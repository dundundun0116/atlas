"""轨迹落盘 / 回放（M0）。

关键验收：**同一条失败记录能在本地重放**——不重新请求模型，只从 trace 重算证据覆盖
与停止指标；换 k 或换金标不产生新 token 成本。
"""

import json

from compassrag.agent import Agent, AgentConfig, AnswerTrace
from compassrag.eval.traces import (
    aggregate,
    derive_row,
    iter_traces,
    replay_rows,
    trace_path,
    write_traces,
)
from compassrag.llm.client import LLMResponse


class FakeTool:
    def __init__(self, ranking=("c1", "c2")):
        self.ranking = list(ranking)

    def search(self, query, k=10):
        return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(self.ranking[:k])]


class FakeLLM:
    def __init__(self, text="Paris"):
        self.text = text
        self.calls = 0

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls += 1
        return LLMResponse(text=self.text, model="fake", prompt_tokens=10, completion_tokens=2,
                           total_tokens=12, latency_ms=1.0, finish_reason="stop")


STORE = {
    "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
    "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
}


def _run(question="Who is Alpha?"):
    llm = FakeLLM("Paris")
    agent = Agent(llm, {"bm25": FakeTool(["c1", "c2"])}, STORE, AgentConfig(top_k=2))
    result = agent.answer(question)
    trace = result.trace
    trace.config["question_id"] = "q1"
    return trace, llm.calls


def test_trace_round_trips_losslessly(tmp_path):
    trace, _ = _run()
    path = write_traces([trace], tmp_path / "t.jsonl")
    back = list(iter_traces(path))
    assert len(back) == 1
    assert back[0].model_dump() == trace.model_dump()


def test_trace_carries_what_replay_needs():
    trace, _ = _run()
    assert trace.queries and trace.queries[0].query == "Who is Alpha?"
    assert trace.tool_calls and trace.tool_calls[0].hits[0].retriever == "bm25"
    assert trace.evidence_ids and trace.evidence_titles == ["Alpha", "Beta"]
    assert trace.stop_reason.value == "answered"
    assert trace.usage.total > 0


def test_derive_row_recomputes_metrics_without_model():
    trace, calls_before = _run()
    row = derive_row(trace, gold="Paris", gold_titles={"Alpha"}, question_id="q1", k=2)
    assert row["em"] == 1.0 and row["f1"] == 1.0
    assert row["ev_recall"] == 1.0 and row["ev_full_hit"] == 1.0
    assert row["stop_reason"] == "answered" and row["rounds"] == 1
    assert row["queries"] == ["Who is Alpha?"] and row["query_sources"] == ["original"]
    assert calls_before == 1  # 派生一行不应该再调模型


def test_replay_can_change_measurement_without_new_tokens(tmp_path):
    trace, _ = _run()
    path = write_traces([trace], tmp_path / "r.jsonl")
    gold = {"Alpha", "Beta"}
    r1 = replay_rows(path, gold_by_id={"q1": "Paris"}, titles_by_id={"q1": gold}, k=1)[0]
    r2 = replay_rows(path, gold_by_id={"q1": "Paris"}, titles_by_id={"q1": gold}, k=2)[0]
    assert r1["ev_recall"] == 0.5 and r2["ev_recall"] == 1.0   # 换 k 只换口径
    assert r1["ev_full_hit"] == 0.0 and r2["ev_full_hit"] == 1.0


def test_aggregate_reports_quality_evidence_cost_reliability():
    trace, _ = _run()
    rows = [derive_row(trace, gold="Paris", gold_titles={"Alpha"}, question_id="q1", k=2)]
    agg = aggregate(rows)
    assert agg["n"] == 1 and agg["em"] == 1.0
    assert agg["ev_full_hit"] == 1.0
    assert agg["tokens_per_q"] > 0 and agg["llm_calls_per_q"] == 1.0
    assert agg["stop_reasons"] == {"answered": 1}


def test_trace_path_is_stable():
    assert trace_path("runs/traces", "musique", "naive").as_posix() == "runs/traces/musique__naive.jsonl"


def test_bad_trace_line_is_skipped(tmp_path):
    good, _ = _run()
    path = tmp_path / "mixed.jsonl"
    path.write_text(good.model_dump_json() + "\n{not json}\n", encoding="utf-8")
    traces = list(iter_traces(path))
    assert len(traces) == 1
