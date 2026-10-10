"""AtlasMemorySystem（MemoryArena 接入形态）测试：wrap/add 循环、边界结转、判分、降级。

LLM 用替身注入，token 计数用空白词计数器，离线可跑（决策 #11–#14）。
"""

import json

from atlas.memory.schema import NodeStatus
from atlas.service import AtlasMemorySystem


def count_words(text: str) -> int:
    return max(1, len(text.split()))


class FakeLLM:
    def __init__(self, reply: str = "{}"):
        self.reply = reply
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def make_system(reply: str = "{}", budget: int = 4000) -> AtlasMemorySystem:
    return AtlasMemorySystem(
        "u1", llm=FakeLLM(reply), token_counter=count_words, budget_tokens=budget
    )


Q1 = "### BACKGROUND:\n论文设定\n### PROBLEM:\n给出 L(h^t,w^t) 的上界"
Q2 = "### BACKGROUND:\nNo information provided.\n### PROBLEM:\n由此证明引理 3"


class TestWrapLoop:
    def test_first_wrap_wraps_prompt_and_registers_subtask(self):
        system = make_system()
        out = system.wrap_user_prompt(Q1)
        assert out.startswith("<memory_context>")
        assert "</memory_context>" in out and "User: " in out
        subs = system.engine.open_subtasks()
        assert len(subs) == 1
        assert subs[0].content == "给出 L(h^t,w^t) 的上界"  # 只存 PROBLEM 部分
        assert system.summaries == []  # 首次 wrap 无结转

    def test_second_wrap_carries_previous_subtask_into_graph_and_summary(self):
        reply = json.dumps({
            "summary": "推导了 L 的上界，采用鞅版本 Lemma 2。",
            "operations": [{"op": "add_node", "type": "fact",
                            "content": "Lemma 2 鞅版本：E[‖ΣX‖²] ≤ 2E[‖Σμ‖²]+2τσ²"}],
        })
        system = make_system(reply)
        system.wrap_user_prompt(Q1)
        system.add_chunk("## Task: t1\n## solution: ans1\n## Judge: INCORRECT")
        out = system.wrap_user_prompt(Q2)
        s1 = system.engine.nodes["S1"]
        assert s1.status == NodeStatus.DONE and s1.meta.get("failed") is True
        assert "曾答错" in out                       # 失败提示进 H 区
        assert "推导了 L 的上界" in out               # LLM 摘要进摘要区
        assert "Lemma 2 鞅版本" in out                # 提取的事实进计划区
        assert "[S2]" in out and "由此证明引理 3" in out  # 当前子任务在计划区
        # 提取器拿到的提示里带下一题（依赖判断的上下文）
        assert "由此证明引理 3" in system.extractor.llm.prompts[-1]
        # 结转审计行（服务端日志的数据源）
        assert system.carry_log and "applied=1" in system.carry_log[-1]

    def test_judge_correct_marks_note_without_failure_hint(self):
        system = make_system()
        system.wrap_user_prompt(Q1)
        system.add_chunk("## Task: t1\n## solution: ans1\n## Judge: CORRECT")
        out = system.wrap_user_prompt(Q2)
        s1 = system.engine.nodes["S1"]
        assert s1.note == "Judge: CORRECT"
        assert not s1.meta.get("failed")
        assert "曾答错" not in out

    def test_missing_judge_line_is_tolerated(self):
        system = make_system()
        system.wrap_user_prompt(Q1)
        system.add_chunk("## Task: t1\n## solution: ans1")  # 无 Judge 行（官方默认口径）
        out = system.wrap_user_prompt(Q2)
        s1 = system.engine.nodes["S1"]
        assert s1.status == NodeStatus.DONE and s1.note == ""
        assert "曾答错" not in out

    def test_pre_first_wrap_noise_is_dropped_without_carry(self):
        """runner 在首个 wrap 前会 add "Initial result: Empty"：不应触发结转/LLM 调用。"""
        system = make_system("不是 JSON")
        system.add_chunk("Initial result: Empty\n")
        out = system.wrap_user_prompt(Q1)
        assert system.carries == 0 and system.carry_log == []
        assert system.extract_errors == []
        assert system.extractor.llm.prompts == []  # 没有烧提取调用
        assert "<memory_context>" in out

    def test_bad_llm_reply_degrades_to_rule_summary(self):
        system = make_system("不是 JSON")
        system.wrap_user_prompt(Q1)
        system.add_chunk("## Task: t1\n## solution: ans1")
        out = system.wrap_user_prompt(Q2)
        assert system.extract_errors            # 降级被记录
        assert "t1" in out                       # 兜底摘要保留原始记录
        assert system.engine.nodes["S1"].status == NodeStatus.DONE  # 规则动作仍生效

    def test_assemble_failure_falls_back_without_raising(self):
        system = make_system(budget=10)  # 计划区 + 各区地板超预算 → PlanZoneOverflow
        out = system.wrap_user_prompt(Q1)
        assert "## 当前计划与约束" in out  # 降级仍给出计划区（状态可见）
        assert system.assemble_errors

    def test_graph_dump_written_on_wrap(self, tmp_path):
        from atlas.memory.engine import GraphMemoryEngine

        dump = tmp_path / "graphs" / "u1.jsonl"
        system = AtlasMemorySystem(
            "u1", llm=FakeLLM(), token_counter=count_words, budget_tokens=4000,
            dump_path=str(dump),
        )
        system.wrap_user_prompt(Q1)
        loaded = GraphMemoryEngine.load_jsonl(dump)  # 快照可被引擎回读（导出工具的数据源）
        assert "S1" in loaded.nodes
