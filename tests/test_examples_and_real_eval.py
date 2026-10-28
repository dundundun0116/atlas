"""Task 5/6：完整链路示例、真实模型研究者与评测入口。

示例必须离线可跑（不依赖密钥），评测入口必须能区分 offline / real 两种口径。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from atlas.evaluation import ModelResearcher, build_policies, load_fixtures, run_matrix
from atlas.storage.repository import GraphRepository, open_database

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import quickstart  # noqa: E402
import research_agent  # noqa: E402


@pytest.fixture(scope="module")
def summary(tmp_path_factory):
    db = tmp_path_factory.mktemp("demo") / "demo.db"
    return research_agent.run_research_demo(str(db))


class TestResearchAgentExample:
    def test_full_chain_reaches_completed(self, summary):
        states = dict(summary["steps"])
        assert states["create"] == "planning"
        assert states["plan"] == "executing"
        assert states["audit"] == "completed"
        assert summary["final_state"] == "completed"

    def test_research_wrote_fact_with_evidence(self, summary):
        assert summary["research_ok"] is True
        assert summary["fact_id"].startswith("F")

    def test_tool_failure_does_not_write_fact(self, summary):
        assert summary["failed_research"]["ok"] is False
        assert summary["failed_research"]["error"]["kind"] == "no_evidence"

    def test_revision_supersedes_old_artifact(self, tmp_path):
        summary = research_agent.run_research_demo(str(tmp_path / "d.db"))
        assert summary["draft_id"]
        # 修订稿作废旧草稿：旧节点保留 status=superseded 与接替指针
        database = open_database(tmp_path / "d.db")
        try:
            repo = GraphRepository(database)
            nodes = {n.node_id: n for n in repo.nodes("demo")}
        finally:
            database.close()
        draft = nodes[summary["draft_id"]]
        assert draft.status == "superseded"
        assert draft.superseded_by

    def test_resume_after_restart_and_replay_ok(self, summary):
        assert summary["replay"]["ok"] is True
        assert summary["replay"]["timeline"], "业务时间线不应为空"
        assert dict(summary["steps"])["resume"] == "executing"

    def test_quickstart_example_runs_offline(self, tmp_path):
        result = quickstart.demo(str(tmp_path / "q.db"))
        assert result["paused"] == "paused"
        assert result["resumed"] == "executing"
        assert result["replay_ok"] is True
        assert result["events"] > 0


class TestModelResearcher:
    def _fixture(self):
        return load_fixtures(("multi_hop",))[0]

    def test_correct_answer_counts_as_completed(self):
        fixture = self._fixture()
        researcher = ModelResearcher(fixture, lambda prompt: "引理 2 给出 L ≤ 2τσ²")
        outcome = researcher.answer(fixture.subtasks[0], "上下文")
        assert outcome["completed"] is True
        assert outcome["violation"] is False

    def test_missing_fact_is_not_completed(self):
        fixture = self._fixture()
        outcome = ModelResearcher(fixture, lambda p: "无法确定").answer(fixture.subtasks[0], "")
        assert outcome["completed"] is False

    def test_stale_value_counts_as_misuse(self):
        fixture = load_fixtures(("conflicting_sources",))[0]
        outcome = ModelResearcher(fixture, lambda p: "常数项为 4τσ²").answer(
            fixture.subtasks[0], "旧值 4τσ²"
        )
        assert outcome["stale_misuse"] is True

    def test_forbidden_method_counts_as_violation(self):
        fixture = load_fixtures(("needs_revision",))[0]
        outcome = ModelResearcher(fixture, lambda p: "只给结论：L ≤ 2τσ²").answer(
            fixture.subtasks[0], ""
        )
        assert outcome["violation"] is True

    def test_model_exception_is_recorded_not_hidden(self):
        fixture = self._fixture()

        def boom(_prompt):
            raise TimeoutError("模型超时")

        outcome = ModelResearcher(fixture, boom).answer(fixture.subtasks[0], "")
        assert outcome["completed"] is False
        assert "TimeoutError" in outcome["error"]

    def test_harness_accepts_injected_researcher(self):
        fixture = self._fixture()
        researcher = ModelResearcher(fixture, lambda p: "引理 2 给出 L ≤ 2τσ²")
        runs = run_matrix([fixture], build_policies(),
                          researcher_factory=lambda f: ModelResearcher(f, researcher.llm))
        assert len(runs) == 3
        assert any(r.policy == "graph_state" for r in runs)


class TestEvalEntryPoint:
    def test_offline_mode_writes_replayable_run_package(self, tmp_path):
        out = tmp_path / "eval_out"
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_runtime_eval.py"),
             "--mode", "offline", "--out", str(out)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
        assert payload["config"]["mode"] == "offline"
        assert "仅验证指标口径" in payload["config"]["honesty_note"]
        assert len(payload["runs"]) == 9

    def test_real_mode_without_credentials_fails_loudly(self, tmp_path):
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_BASE_URL", "OPENAI_API_KEY")}
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_runtime_eval.py"),
             "--mode", "real", "--out", str(tmp_path / "x")],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert result.returncode != 0
        assert "OPENAI_BASE_URL" in (result.stderr + result.stdout)


class TestTokenCounter:
    def test_tiktoken_counter_is_available_and_differs_from_words(self):
        import sys as _sys

        _sys.path.insert(0, str(ROOT / "scripts"))
        from run_runtime_eval import make_token_counter

        words = make_token_counter("words")
        tokens = make_token_counter("tiktoken")
        text = "引理 2 给出 L ≤ 2τσ²（来源 Theorem 3）"
        assert tokens(text) > 0
        assert tokens(text) != words(text)   # 词数只是代理，真 token 才能进简历

    def test_arg_parser_exposes_long_steps_and_tokenizer(self):
        import sys as _sys

        _sys.path.insert(0, str(ROOT / "scripts"))
        from run_runtime_eval import build_arg_parser

        args = build_arg_parser().parse_args(
            ["--mode", "real", "--families", "long_horizon", "--long-steps", "40",
             "--tokenizer", "tiktoken"]
        )
        assert (args.long_steps, args.tokenizer) == (40, "tiktoken")
