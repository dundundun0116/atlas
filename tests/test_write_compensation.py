"""可控写工具与补偿协议（设计文档 §4.8 / §5.5 / §13）。

只读检索工具永远造不出"部分生效"和"重复副作用"，所以用 `ControllableWriteTool`
提供一个**可检查的外部世界**，把这些现场真的跑出来。
"""

import pytest

from atlas.storage.repository import GraphRepository, open_database
from atlas.tools.calls import EffectClass, ToolSpec, ToolCallStatus
from atlas.tools.compensation import (
    decide_after_call,
    requires_human_approval,
    run_compensation,
)
from atlas.tools.dispatcher import ToolDispatcher
from atlas.tools.writes import (
    WRITE_TOOL_SPEC,
    ControllableWriteTool,
    WriteToolError,
)


@pytest.fixture()
def repo(tmp_path):
    database = open_database(tmp_path / "writes.db")
    repository = GraphRepository(database)
    repository.create_session("s1", goal="g")
    yield repository
    database.close()


def _dispatcher(repo, spec=WRITE_TOOL_SPEC):
    return ToolDispatcher(repo, "s1", spec, step_id="write")


class TestWriteToolModes:
    def test_ok_writes_and_returns(self):
        tool = ControllableWriteTool()
        assert tool.write("k", {"a": 1}) == {"applied": {"a": 1}}
        assert tool.world == {"k": {"a": 1}}

    def test_fail_does_not_touch_the_world(self):
        tool = ControllableWriteTool(mode="fail")
        with pytest.raises(WriteToolError) as exc:
            tool.write("k", {"a": 1})
        assert exc.value.kind == "invalid_request"
        assert tool.world == {}

    def test_apply_then_timeout_leaves_a_trace(self):
        """最典型的结果未知：写进去了，但调用方只看到超时。"""
        tool = ControllableWriteTool(mode="apply_then_timeout")
        with pytest.raises(WriteToolError) as exc:
            tool.write("k", {"a": 1})
        assert exc.value.kind == "timeout"
        assert tool.world == {"k": {"a": 1}}
        assert tool.lookup("k") == {"applied": {"a": 1}}

    def test_partial_writes_only_half(self):
        tool = ControllableWriteTool(mode="partial")
        with pytest.raises(WriteToolError):
            tool.write("k", {"a": 1, "b": 2, "c": 3, "d": 4})
        assert set(tool.world["k"]) == {"a", "b"}

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            ControllableWriteTool(mode="whatever")


class TestDispatcherWithWrites:
    def test_ok_settles_applied(self, repo):
        tool = ControllableWriteTool()
        outcome = _dispatcher(repo).invoke(lambda: tool.write("k", {"a": 1}), {"key": "k"})
        assert outcome.ok is True
        assert tool.world == {"k": {"a": 1}}

    def test_timeout_after_write_is_indeterminate(self, repo):
        tool = ControllableWriteTool(mode="apply_then_timeout")
        outcome = _dispatcher(repo).invoke(lambda: tool.write("k", {"a": 1}), {"key": "k"})
        assert outcome.status is ToolCallStatus.INDETERMINATE
        assert outcome.needs_human
        # 世界其实已经变了——所以不能重发
        assert tool.world == {"k": {"a": 1}}

    def test_partial_is_failed_partial(self, repo):
        tool = ControllableWriteTool(mode="partial")
        outcome = _dispatcher(repo).invoke(
            lambda: tool.write("k", {"a": 1, "b": 2, "c": 3, "d": 4}), {"key": "k"})
        assert outcome.status is ToolCallStatus.FAILED_PARTIAL
        assert outcome.outcome == "partially_applied"
        assert outcome.needs_compensation is True
        assert outcome.retryable is False

    def test_indeterminate_can_be_reconciled_by_lookup(self, repo):
        tool = ControllableWriteTool(mode="apply_then_timeout")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(lambda: tool.write("k", {"a": 1}), {"key": "k"})
        # 真实服务按**幂等键**索引，这里由测试替身把它映射到自己的存储键
        fixed = dispatcher.reconcile(
            outcome.tool_call_id,
            lookup=lambda idem_key: tool.lookup("k"),
            observe=lambda: tool.observe("k"),
        )
        assert fixed.status is ToolCallStatus.RECONCILED
        assert fixed.outcome == "applied"
        assert fixed.result == {"applied": {"a": 1}}


class TestCompensationDecision:
    def test_settled_applied_needs_nothing(self):
        decision = decide_after_call(
            {"status": "settled", "outcome": "applied"}, WRITE_TOOL_SPEC)
        assert decision.action == "none"

    def test_partial_asks_for_compensation(self):
        decision = decide_after_call(
            {"status": "failed_partial", "outcome": "partially_applied"},
            WRITE_TOOL_SPEC, compensation_args={"key": "k"})
        assert decision.action == "compensate"
        assert decision.compensation_args == {"key": "k"}

    def test_partial_without_capability_goes_human(self):
        spec = ToolSpec(name="x", effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
                        supports_compensation=False)
        decision = decide_after_call(
            {"status": "failed_partial", "outcome": "partially_applied"},
            spec, compensation_args={"key": "k"})
        assert decision.action == "human"
        assert "不支持补偿" in decision.reason

    def test_partial_without_args_goes_human(self):
        decision = decide_after_call(
            {"status": "failed_partial", "outcome": "partially_applied"}, WRITE_TOOL_SPEC)
        assert decision.action == "human"

    def test_unknown_with_lookup_reconciles_else_human(self):
        with_lookup = decide_after_call({"status": "indeterminate"}, WRITE_TOOL_SPEC)
        assert with_lookup.action == "reconcile"

        blind = ToolSpec(name="x", supports_lookup=False)
        assert decide_after_call({"status": "indeterminate"}, blind).action == "human"

    def test_determinate_failure_retries_only_when_safe(self):
        retry = decide_after_call(
            {"status": "failed_determinate", "outcome": "not_applied"}, WRITE_TOOL_SPEC)
        assert retry.action == "retry"

        risky = ToolSpec(name="x", effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
                         supports_idempotency=False)
        assert decide_after_call(
            {"status": "failed_determinate", "outcome": "not_applied"}, risky
        ).action == "human"

    def test_high_risk_tool_needs_approval_before_dispatch(self):
        assert requires_human_approval(
            ToolSpec(name="x", effect_class=EffectClass.EXTERNAL_SIDE_EFFECT,
                     supports_compensation=False)) is True
        assert requires_human_approval(WRITE_TOOL_SPEC) is False
        assert requires_human_approval(
            ToolSpec(name="x", effect_class=EffectClass.READ_ONLY)) is False


class TestCompensationFlow:
    def test_partial_then_compensate_restores_the_world(self, repo):
        tool = ControllableWriteTool(mode="partial")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(
            lambda: tool.write("o1", {"a": 1, "b": 2, "c": 3}), {"key": "o1"})
        call = repo.get_tool_call("s1", outcome.tool_call_id)

        decision = decide_after_call(call, WRITE_TOOL_SPEC,
                                     compensation_args={"key": "o1"})
        result = run_compensation(
            repo, "s1", call, WRITE_TOOL_SPEC,
            compensation_args=decision.compensation_args,
            perform=lambda args: tool.undo(args["key"]),
        )

        assert result["status"] == "settled"
        assert result["outcome"] == "applied"
        assert tool.world == {}, "补偿必须真的把副作用撤掉"

    def test_original_call_history_is_untouched(self, repo):
        tool = ControllableWriteTool(mode="partial")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(lambda: tool.write("o1", {"a": 1, "b": 2}),
                                    {"key": "o1"})
        call = repo.get_tool_call("s1", outcome.tool_call_id)
        run_compensation(repo, "s1", call, WRITE_TOOL_SPEC,
                         compensation_args={"key": "o1"},
                         perform=lambda args: tool.undo(args["key"]))

        original = repo.get_tool_call("s1", outcome.tool_call_id)
        assert original["status"] == "failed_partial"
        assert original["outcome"] == "partially_applied"
        assert original["compensation_for"] == ""

    def test_compensation_is_linked_to_the_original(self, repo):
        tool = ControllableWriteTool(mode="partial")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(lambda: tool.write("o1", {"a": 1, "b": 2}),
                                    {"key": "o1"})
        call = repo.get_tool_call("s1", outcome.tool_call_id)
        result = run_compensation(repo, "s1", call, WRITE_TOOL_SPEC,
                                  compensation_args={"key": "o1"},
                                  perform=lambda args: tool.undo(args["key"]))

        compensation = repo.get_tool_call("s1", result["compensation_id"])
        assert compensation["compensation_for"] == outcome.tool_call_id
        assert compensation["tool_call_id"] != outcome.tool_call_id
        assert len(repo.tool_calls("s1")) == 2

    def test_running_compensation_twice_reuses_the_same_call(self, repo):
        tool = ControllableWriteTool(mode="partial")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(lambda: tool.write("o1", {"a": 1, "b": 2}),
                                    {"key": "o1"})
        call = repo.get_tool_call("s1", outcome.tool_call_id)
        first = run_compensation(repo, "s1", call, WRITE_TOOL_SPEC,
                                 compensation_args={"key": "o1"},
                                 perform=lambda args: tool.undo(args["key"]))
        second = run_compensation(repo, "s1", call, WRITE_TOOL_SPEC,
                                  compensation_args={"key": "o1"},
                                  perform=lambda args: tool.undo(args["key"]))
        assert first["compensation_id"] == second["compensation_id"]
        assert second["resolved"] is False, "第二次命中了幂等键，不该再产生一条"
        assert len(repo.tool_calls("s1")) == 2

    def test_failed_compensation_is_determinate_not_unknown(self, repo):
        tool = ControllableWriteTool(mode="partial")
        dispatcher = _dispatcher(repo)
        outcome = dispatcher.invoke(lambda: tool.write("o1", {"a": 1, "b": 2}),
                                    {"key": "o1"})
        call = repo.get_tool_call("s1", outcome.tool_call_id)

        def boom(_args):
            raise WriteToolError("invalid_request", "原对象已不存在")

        result = run_compensation(repo, "s1", call, WRITE_TOOL_SPEC,
                                  compensation_args={"key": "o1"}, perform=boom)
        assert result["status"] == "failed_determinate"
        assert result["outcome"] == "not_applied", "补偿失败是确定结论，不是未知"
