"""离线端到端冒烟：引擎 → 结转触发 → 结转 → 组装 → 查账，全链路组合验证。

（组合冒烟：不引入新生产代码，验证既有模块按设计协同。）
"""

from atlas.context.assembler import ContextAssembler
from atlas.context.carryover import CarryOverManager, CarryOverTrigger
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType


def count_words(text: str) -> int:
    return max(1, len(text.split()))


class FakeExtractor:
    def __call__(self, recent_raw, engine):
        engine.add_fact("展会期间西湖区房价翻倍", actor="curator")


def test_full_pipeline_offline():
    eng = GraphMemoryEngine()
    eng.add_goal("规划杭州 3 日行")
    s1 = eng.add_subtask("定日期")
    s2 = eng.add_subtask("订酒店")
    eng.link(s2, s1, EdgeType.DEPENDS_ON)
    eng.add_constraint("总预算 5000 元")

    assembler = ContextAssembler(eng, token_counter=count_words,
                                 floors={"summary": 5, "recent": 5, "hints": 2})
    trigger = CarryOverTrigger(eng, token_counter=count_words, budget_tokens=500)
    manager = CarryOverManager(eng, token_counter=count_words)

    # 第 1 阶段：S1 完成 → 边界触发结转
    eng.complete_subtask(s1.id)
    assert trigger.should_carry(["查询结果：5 月 14-16 日有票"]) is True
    summaries = manager.execute(["查询结果：5 月 14-16 日有票"], extractor=FakeExtractor())
    trigger.mark_carried()
    assert summaries[-1] == "（子目标边界：S1 定日期 完成）"
    assert "展会期间西湖区房价翻倍" in [f.content for f in eng.nodes.values()]

    # 组装：四区齐全、查账通过；提取出的事实进计划区（执行状态必须可见）
    result = assembler.assemble(summaries, ["action: 查滨江区酒店"], budget_tokens=2000)
    plan = result.text.split("## 已完成工作摘要")[0]
    assert "展会期间西湖区房价翻倍" in plan
    assert result.audit_ok
    # 此时无作废/过期，提示区为空是正确行为
    assert "（无）" in result.text.split("## 踩坑与提示")[1]
    # 边界之后的用量不再触发
    assert trigger.should_carry(["零星记录"]) is False

    # 第 2 阶段：预算约束被推翻 → 新约束进计划区、旧约束进提示区
    c2 = eng.supersede_with("C1", new_content="总预算 6000 元（用户上调）", reason="用户明确同意")
    result2 = assembler.assemble(summaries, ["action: 继续订酒店"], budget_tokens=2000)
    plan = result2.text.split("## 已完成工作摘要")[0]
    assert "[C2] 总预算 6000 元（用户上调）" in plan
    assert "[C1]" not in plan
    assert "已作废" in result2.text.split("## 踩坑与提示")[1]
