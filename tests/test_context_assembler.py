"""四区上下文组装器测试（决策清单 #3 规则查账、#4 四区+动态预算）。

token 计数用注入的空白词计数器，测试离线可跑；生产用 tiktoken。
"""

import pytest

from atlas.context.assembler import ContextAssembler, PlanZoneOverflow
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType


def count_words(text: str) -> int:
    return max(1, len(text.split()))


@pytest.fixture()
def engine():
    eng = GraphMemoryEngine()
    eng.add_goal("规划杭州 3 日行")
    s3 = eng.add_subtask("定日期")
    s1 = eng.add_subtask("订机票")
    s2 = eng.add_subtask("订酒店")
    eng.link(s1, s3, EdgeType.DEPENDS_ON)
    eng.complete_subtask(s3.id)
    eng.add_constraint("总预算 5000 元")
    d1 = eng.add_decision("住西湖区", reason="离景点近")
    eng.supersede_with(d1.id, new_content="住滨江区", reason="展会房价翻倍")
    return eng


@pytest.fixture()
def assembler(engine):
    return ContextAssembler(
        engine, token_counter=count_words,
        floors={"summary": 5, "recent": 5, "hints": 2},
    )


SUMMARIES = [
    "[步骤 2] S3 定日期完成：确定为 5 月 14-16 日",
    "[步骤 5] 查询西湖区酒店：均价 800 元/晚",
    "[步骤 8] S1 订机票：已出票",
]
RECENT = [
    "action: search[滨江区酒店]",
    "obs: 找到 3 家，最低 400 元/晚",
]


class TestZones:
    def test_four_zones_in_order(self, assembler):
        text = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        p1 = text.index("## 当前计划与约束")
        p2 = text.index("## 已完成工作摘要")
        p3 = text.index("## 近期原始记录")
        p4 = text.index("## 踩坑与提示")
        assert p1 < p2 < p3 < p4

    def test_plan_zone_lists_goal_subtasks_constraints_decisions(self, assembler):
        text = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        plan = text.split("## 已完成工作摘要")[0]
        assert "规划杭州 3 日行" in plan
        # fixture 里 S1=定日期(已完成)、S2=订机票、S3=订酒店；S2 依赖 S1
        assert "[S2] 订机票" in plan
        assert "依赖 S1" in plan
        assert "总预算 5000 元" in plan and "[C1]" in plan
        assert "住滨江区" in plan and "[D2]" in plan
        assert "住西湖区" not in plan  # 已作废的决定不进计划区

    def test_plan_zone_lists_active_facts_and_artifacts(self, engine):
        eng = GraphMemoryEngine()
        eng.add_goal("g")
        eng.add_fact("杭州东站有地铁 1 号线")
        eng.add_artifact("行程单.md")
        assembler = ContextAssembler(eng, token_counter=count_words)
        plan = assembler.assemble([], [], budget_tokens=10000).text.split("## 已完成工作摘要")[0]
        assert "[F1] 杭州东站有地铁 1 号线" in plan
        assert "[A1] 行程单.md" in plan

    def test_focus_keeps_related_facts_and_always_keeps_constraints(self):
        eng = GraphMemoryEngine()
        eng.add_goal("g")
        eng.add_constraint("必须使用最新版本")
        eng.add_fact("参数 k1 = 1.75")
        eng.add_fact("参数 k2 = 2.5")
        assembler = ContextAssembler(eng, token_counter=count_words)
        text = assembler.assemble([], [], budget_tokens=1000, focus="参数 k2 的取值").text
        assert "参数 k2 = 2.5" in text
        assert "参数 k1 = 1.75" not in text
        assert "必须使用最新版本" in text

    def test_hints_zone_carries_pitfalls(self, assembler):
        text = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        hints = text.split("## 踩坑与提示")[1]
        assert "住西湖区" in hints and "展会房价翻倍" in hints


class TestAudit:
    def test_audit_passes_when_plan_zone_intact(self, assembler):
        result = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000)
        assert result.audit_ok is True

    def test_plan_zone_overflow_raises(self, engine):
        eng = GraphMemoryEngine()
        eng.add_goal("g")
        for i in range(5):
            eng.add_constraint(f"约束{i}：" + "很长很长的约束内容 " * 20)
        assembler = ContextAssembler(eng, token_counter=count_words)
        with pytest.raises(PlanZoneOverflow):
            assembler.assemble([], [], budget_tokens=50)


class TestDynamicBudget:
    def test_under_budget_keeps_everything(self, assembler):
        text = assembler.assemble(
            SUMMARIES * 2, RECENT, budget_tokens=100000
        ).text
        for s in SUMMARIES * 2:
            assert s in text
        assert "找到 3 家" in text

    def test_over_budget_trims_summary_oldest_first(self, assembler):
        many = [f"[步骤 {i}] 摘要条目 " + "内容 " * 30 for i in range(1, 11)]
        text = assembler.assemble(many, RECENT, budget_tokens=300).text
        summary_zone = text.split("## 已完成工作摘要")[1].split("## 近期原始记录")[0]
        assert "[步骤 10]" in summary_zone   # 最新的摘要保留
        assert "[步骤 1]" not in summary_zone  # 最旧的摘要被裁掉

    def test_over_budget_keeps_recent_raw_tail(self, assembler):
        raws = [f"action: 第{i}步操作" for i in range(1, 41)]
        text = assembler.assemble(SUMMARIES, raws, budget_tokens=110).text
        recent_zone = text.split("## 近期原始记录")[1].split("## 踩坑与提示")[0]
        assert "第40步操作" in recent_zone
        assert "第1步操作" not in recent_zone


class TestPrefixStability:
    def test_state_unchanged_output_deterministic(self, assembler):
        t1 = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        t2 = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        assert t1 == t2

    def test_appending_recent_raw_keeps_prefix(self, assembler):
        t1 = assembler.assemble(SUMMARIES, RECENT, budget_tokens=10000).text
        t2 = assembler.assemble(SUMMARIES, RECENT + ["action: 新的一步"], budget_tokens=10000).text
        head1 = t1.split("## 近期原始记录")[0]
        head2 = t2.split("## 近期原始记录")[0]
        assert head1 == head2
        assert t2.startswith(head1)
