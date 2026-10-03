"""结转触发与管理测试（决策清单 #2：子目标边界优先 + 80% 阈值兜底）。"""

import pytest

from atlas.context.carryover import CarryOverManager, CarryOverTrigger
from atlas.memory.engine import GraphMemoryEngine


def count_words(text: str) -> int:
    return max(1, len(text.split()))


@pytest.fixture()
def engine():
    eng = GraphMemoryEngine()
    eng.add_goal("规划杭州 3 日行")
    eng.add_subtask("定日期")
    eng.add_subtask("订机票")
    return eng


class TestTrigger:
    def test_no_boundary_low_usage_is_false(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=1000)
        assert trigger.should_carry(["一条普通记录"]) is False

    def test_subtask_boundary_triggers(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=1000)
        engine.complete_subtask("S1")
        assert trigger.should_carry(["一条普通记录"]) is True

    def test_mark_carried_resets_boundary(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=1000)
        engine.complete_subtask("S1")
        trigger.mark_carried()
        assert trigger.should_carry(["一条普通记录"]) is False

    def test_second_boundary_retriggers_after_mark(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=1000)
        engine.complete_subtask("S1")
        trigger.mark_carried()
        engine.complete_subtask("S2")
        assert trigger.should_carry(["一条普通记录"]) is True

    def test_threshold_triggers_without_boundary(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=10)
        # usage = 9/10 = 90% >= 80%，无边界也必须结转
        assert trigger.should_carry(["a b c d e f g h i"]) is True

    def test_threshold_edge_exactly_80(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=10)
        assert trigger.should_carry(["a b c d e f g h"]) is True  # 8/10 = 80%

    def test_below_threshold_no_boundary_false(self, engine):
        trigger = CarryOverTrigger(engine, token_counter=count_words, budget_tokens=100)
        assert trigger.should_carry(["a b c"]) is False


class FakeExtractor:
    """结转提取器替身：真实实现用 LLM 从原始记录提取状态写图；测试验证调用协议。"""

    def __init__(self):
        self.calls = []

    def __call__(self, recent_raw, engine):
        self.calls.append(list(recent_raw))
        engine.add_fact("从最近记录提取的环境事实", actor="curator")


class TestManager:
    def test_step_numbers_continuous_across_carries(self, engine):
        manager = CarryOverManager(engine, token_counter=count_words)
        s1 = manager.execute(["记录一", "记录二", "记录三"])
        s2 = manager.execute(["记录四", "记录五"])
        assert s1 == ["[步骤 1] 记录一", "[步骤 2] 记录二", "[步骤 3] 记录三"]
        assert s2 == ["[步骤 4] 记录四", "[步骤 5] 记录五"]

    def test_runs_extractor_before_summarizing(self, engine):
        extractor = FakeExtractor()
        manager = CarryOverManager(engine, token_counter=count_words)
        manager.execute(["记录一"], extractor=extractor)
        assert extractor.calls == [["记录一"]]
        assert any(f.content == "从最近记录提取的环境事实" for f in engine.nodes.values())

    def test_notes_boundary_subtasks_once(self, engine):
        manager = CarryOverManager(engine, token_counter=count_words)
        engine.complete_subtask("S1")
        first = manager.execute(["记录一"])
        second = manager.execute(["记录二"])
        assert "S1 定日期 完成" in first[-1]
        assert not any("完成" in line for line in second)  # 边界只报一次

    def test_empty_raw_returns_empty_summary(self, engine):
        manager = CarryOverManager(engine, token_counter=count_words)
        assert manager.execute([]) == []
