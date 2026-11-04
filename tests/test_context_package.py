"""ContextPackage：每次模型调用的输入快照（设计文档 §4.12 / §7.2）。

这里断言的是「可复盘」的三件事：
1. 模型看到的内容与四区组装器一致（不是"最近 5 个节点"摘要）；
2. 每次调用都留下 digest / 分区 token / 包含与排除的引用；
3. 组装不变量被破坏时**降级并留痕**，不静默当成完整上下文。
"""

import pytest

from atlas.context.builder import ContextPackage, build_context_package, digest_text
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database


def count_words(text: str) -> int:
    return len(str(text or "").split())


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "ctx.db")
    yield database
    database.close()


@pytest.fixture()
def repo(db):
    return GraphRepository(db)


def _engine_with_state():
    engine = GraphMemoryEngine()
    engine.add_goal("规划杭州 3 日行")
    subtask = engine.add_subtask("订票")
    engine.link(subtask, "G1", EdgeType.SUBTASK_OF)
    engine.add_constraint("预算 8000", source="user", valid_until="2099-01-01")
    engine.add_decision("住西湖区", reason="近", source="planner")
    engine.add_fact("高铁 2 小时", source="research_tool", confidence=0.9)
    return engine


class TestBuildContextPackage:
    def test_renders_four_zone_state_not_truncated_preview(self):
        package = build_context_package(
            _engine_with_state(), role="worker", session_id="s1",
            state_version=4, budget_tokens=4000, token_counter=count_words,
            tokenizer_name="words",
        )
        assert package.sections["plan"] > 0
        # 计划区必须带出真实状态：目标、约束、决定、事实都在里面
        assert "规划杭州 3 日行" in package.text
        assert "预算 8000" in package.text
        assert "住西湖区" in package.text
        assert package.included_refs  # [C1] / [D1] / [F1] 之类的节点引用
        assert package.digest == digest_text(package.text)
        assert package.actual_tokens > 0
        assert package.tokenizer == "words"
        assert package.state_version == 4
        assert package.degraded is False

    def test_excluded_refs_show_what_the_model_cannot_see(self):
        engine = _engine_with_state()
        engine.add_constraint("预算 5000", source="user", valid_until="2000-01-01",
                              meta={"key": "budget"})
        package = build_context_package(
            engine, role="planner", budget_tokens=4000, token_counter=count_words,
            tokenizer_name="words",
        )
        excluded = {item["id"]: item["reason"] for item in package.excluded_refs}
        assert excluded, "过期约束必须出现在 excluded_refs，而不是悄悄消失"
        assert any("过期" in reason for reason in excluded.values())

    def test_degraded_packate_when_plan_zone_overflows(self):
        engine = _engine_with_state()
        package = build_context_package(
            engine, role="worker", budget_tokens=5, token_counter=count_words,
            tokenizer_name="words",
        )
        assert package.degraded is True
        assert package.warnings and "PlanZoneOverflow" in package.warnings[0]
        # 降级也要给出计划区，保证状态可见
        assert "规划杭州 3 日行" in package.text

    def test_unknown_zone_is_rejected(self):
        with pytest.raises(ValueError):
            build_context_package(
                _engine_with_state(), role="worker", token_counter=count_words,
                zones=("summary", "nope"),
            )

    def test_digest_is_stable_for_same_state(self):
        first = build_context_package(
            _engine_with_state(), role="worker", budget_tokens=4000,
            token_counter=count_words, tokenizer_name="words",
        )
        second = build_context_package(
            _engine_with_state(), role="worker", budget_tokens=4000,
            token_counter=count_words, tokenizer_name="words",
        )
        assert first.digest == second.digest
        assert first.context_id != second.context_id


class TestContextPackagePersistence:
    def test_build_context_persists_digest_and_refs(self, repo):
        repo.create_session("s1", goal="规划杭州 3 日行")
        runtime = ServiceRuntime(repo, "s1")
        runtime.note("planner", "先订票再订酒店", kind="decision", reason="先远后近")
        runtime.note("research", "高铁 2 小时", kind="fact", source="compassrag", confidence=0.9)

        package = runtime.build_context("worker", token_counter=count_words)

        assert isinstance(package, ContextPackage)
        stored = repo.context_packages("s1")
        assert len(stored) == 1
        assert stored[0]["digest"] == package.digest
        assert stored[0]["role"] == "worker"
        assert stored[0]["actual_tokens"] == package.actual_tokens
        assert "C1" in stored[0]["included_refs"] or "F1" in stored[0]["included_refs"]

    def test_persist_can_be_disabled(self, repo):
        repo.create_session("s1", goal="g")
        runtime = ServiceRuntime(repo, "s1")
        runtime.build_context("planner", token_counter=count_words, persist=False)
        assert repo.context_packages("s1") == []
