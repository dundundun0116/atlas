"""MAGE 简化基线（MAGELiteMemorySystem）：叙事摘要记忆，Atlas 的中间对照臂。

复现边界（精读笔记 #8）：无树、无 Maintain、无 Revise——Update = 子任务边界用 LLM
把上一子任务原始记录压缩成一条叙事摘要（C 区累计）、清近期区；Retrieve = 返回 C+R，
在 math 域每子任务 1 add 的节奏下 R 恒空，即纯摘要基线。与 Atlas 臂同边界结转、同
LLM，唯一差异是记忆形态（叙事摘要 vs 结构化图）——这正是对照要测的假设。
"""

import pytest

from atlas.mage_baseline import MAGELiteMemorySystem

RECORD = "## Task: 子任务一\n## solution: 答案 42\n## Judge: CORRECT\n"


def _counter(text: str) -> int:
    return max(1, len(text) // 4)


class EchoLLM:
    """摘要替身：返回固定文本并记录调用。"""

    def __init__(self, reply: str = "摘要内容"):
        self.calls: list = []
        self.reply = reply

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.reply


def test_first_wrap_has_no_history_and_no_llm_call():
    llm = EchoLLM()
    s = MAGELiteMemorySystem("u1", llm=llm, token_counter=_counter)
    out = s.wrap_user_prompt("### PROBLEM:\n题目一")
    assert llm.calls == []  # 无历史可摘要，不烧调用
    assert out.startswith("<memory_context>") and "题目一" in out
    assert "（无）" in out


def test_initial_empty_record_is_dropped_not_summarized():
    llm = EchoLLM()
    s = MAGELiteMemorySystem("u1", llm=llm, token_counter=_counter)
    s.add_chunk("Initial result: Empty\n")
    s.wrap_user_prompt("### PROBLEM:\n题目一")  # 首个 wrap 前的噪声，直接丢弃
    s.add_chunk(RECORD)
    s.wrap_user_prompt("### PROBLEM:\n题目二")
    assert len(llm.calls) == 1 and "Initial result" not in llm.calls[0]


def test_second_wrap_summarizes_previous_record():
    llm = EchoLLM(reply="得到答案 42，判定正确")
    s = MAGELiteMemorySystem("u1", llm=llm, token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    s.add_chunk(RECORD)
    out = s.wrap_user_prompt("### PROBLEM:\n题目二")
    assert len(llm.calls) == 1 and "答案 42" in llm.calls[0]  # 摘要输入 = 原始记录
    assert "得到答案 42，判定正确" in out  # 摘要进入组装上下文
    assert s.summaries == ["得到答案 42，判定正确"]
    assert s.buffer == []  # 清近期区


def test_summaries_accumulate_one_per_boundary():
    llm = EchoLLM()
    s = MAGELiteMemorySystem("u1", llm=llm, token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    for i in range(2, 4):
        s.add_chunk(RECORD)
        s.wrap_user_prompt(f"### PROBLEM:\n题目{i}")
    assert len(s.summaries) == 2 and len(llm.calls) == 2


def test_empty_reply_falls_back_to_truncated_record():
    s = MAGELiteMemorySystem("u1", llm=EchoLLM(reply=""), token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    s.add_chunk(RECORD)
    out = s.wrap_user_prompt("### PROBLEM:\n题目二")
    assert "答案 42" in out and s.fallbacks == 1  # 降级为记录原文（截断）


def test_llm_exception_falls_back_and_keeps_running():
    class BoomLLM:
        def __call__(self, prompt: str) -> str:
            raise RuntimeError("boom")

    s = MAGELiteMemorySystem("u1", llm=BoomLLM(), token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    s.add_chunk(RECORD)
    out = s.wrap_user_prompt("### PROBLEM:\n题目二")  # 不抛
    assert "答案 42" in out and s.fallbacks == 1
    assert s.extract_errors  # 降级原因可审计


def test_fallback_is_bounded_for_long_record():
    long_text = "数值 1234567890 " * 200
    s = MAGELiteMemorySystem("u1", llm=EchoLLM(reply=""), token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    s.add_chunk(f"## Task: t\n## solution: {long_text}\n")
    out = s.wrap_user_prompt("### PROBLEM:\n题目二")
    assert len(out) < 4000  # 降级摘要有界，上下文不失控


def test_audit_lines_feed_server_log_hooks():
    """与 Atlas 同名的 carry_log/extract_errors 让服务端 _flush_events 零改动复用。"""
    s = MAGELiteMemorySystem("u1", llm=EchoLLM(), token_counter=_counter)
    s.wrap_user_prompt("### PROBLEM:\n题目一")
    s.add_chunk(RECORD)
    s.wrap_user_prompt("### PROBLEM:\n题目二")
    assert len(s.carry_log) == 1 and not s.extract_errors
