"""LLM 结转提取器测试（决策 #12：规则+LLM 六类混合；LLM 以替身注入，离线可跑）。"""

import json

from atlas.context.extractor import ExtractionResult, LLMCarryOverExtractor
from atlas.memory.engine import GraphMemoryEngine
from atlas.memory.schema import EdgeType, NodeStatus, NodeType


class FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def make_engine() -> GraphMemoryEngine:
    eng = GraphMemoryEngine()
    eng.add_goal("证明论文主定理")
    eng.add_subtask("给出 L(h^t,w^t) 的上界")
    return eng


def reply_with(ops, summary="本段完成了上界推导。"):
    return json.dumps({"summary": summary, "operations": ops})


RAW = ["## Task: 给出 L 的上界\n## solution: 由 Lemma 2 得 L ≤ 4γL·E[f(x^0)]\n## Judge: CORRECT"]


class TestApplyOps:
    def test_adds_semantic_nodes_with_links(self):
        eng = make_engine()
        llm = FakeLLM(reply_with([
            {"op": "add_node", "type": "constraint", "content": "γL ≤ 1/24", "constrains": "S1"},
            {"op": "add_node", "type": "decision", "content": "采用 Lemma 2 的鞅版本", "reason": "变量非独立"},
            {"op": "add_node", "type": "artifact", "content": "L ≤ 4γL·E[f(x^0)]", "decided_by": "D1"},
        ]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert res.error == ""
        assert res.summary == "本段完成了上界推导。"
        assert len(res.ops_applied) == 3
        by_type = {n.type: n for n in eng.nodes.values()}
        assert by_type[NodeType.CONSTRAINT].content == "γL ≤ 1/24"
        assert by_type[NodeType.DECISION].reason == "变量非独立"
        assert by_type[NodeType.ARTIFACT].content == "L ≤ 4γL·E[f(x^0)]"
        # 边：C1 --constrains--> S1；A1 --decided_by--> D1
        assert any(
            e.src == "C1" and e.dst == "S1" and e.type == EdgeType.CONSTRAINS
            for e in eng.edges
        )
        assert any(
            e.src == "A1" and e.dst == "D1" and e.type == EdgeType.DECIDED_BY
            for e in eng.edges
        )

    def test_fenced_json_is_parsed(self):
        eng = make_engine()
        reply = "```json\n" + reply_with(
            [{"op": "add_node", "type": "fact", "content": "Lemma 2 给出鞅版本界"}]
        ) + "\n```"
        res = LLMCarryOverExtractor(FakeLLM(reply)).extract(RAW, eng)
        assert res.error == ""
        assert any(n.content == "Lemma 2 给出鞅版本界" for n in eng.nodes.values())

    def test_bad_json_degrades_without_touching_graph(self):
        eng = make_engine()
        before = set(eng.nodes)
        res = LLMCarryOverExtractor(FakeLLM("抱歉，我无法完成。")).extract(RAW, eng)
        assert res.error
        assert res.summary == ""
        assert set(eng.nodes) == before
        assert eng.edges == []

    def test_supersede_replaces_and_records_reason(self):
        eng = make_engine()
        eng.add_fact("σ² 的界是 τσ²")  # F1
        llm = FakeLLM(reply_with([
            {"op": "supersede", "target": "F1", "content": "σ² 的界是 τ²σ²", "reason": "鞅版本给出更紧的界"},
        ]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert res.error == ""
        assert eng.nodes["F1"].status == NodeStatus.SUPERSEDED
        new = eng.nodes[eng.nodes["F1"].superseded_by]
        assert new.content == "σ² 的界是 τ²σ²"
        assert "鞅版本" in new.note

    def test_invalid_ops_are_skipped_others_applied(self):
        eng = make_engine()
        llm = FakeLLM(reply_with([
            {"op": "add_node", "type": "subtask", "content": "越权建子任务"},      # 子任务归规则管
            {"op": "add_node", "type": "fact", "content": "   "},                 # 空内容
            {"op": "add_node", "type": "decision", "content": "无原因的决定"},      # 缺 reason
            {"op": "add_edge", "src": "S1", "dst": "S9", "type": "depends_on"},  # 不存在的 id
            {"op": "explode", "target": "F1"},                                    # 未知操作
            {"op": "add_node", "type": "fact", "content": "L 是 L-smooth 常数"},  # 唯一合法
        ]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert len(res.ops_applied) == 1
        assert len(res.ops_skipped) == 5
        assert any(n.content == "L 是 L-smooth 常数" for n in eng.nodes.values())

    def test_depends_on_cycle_is_rejected(self):
        eng = make_engine()
        s2 = eng.add_subtask("第二个子任务")
        eng.link("S1", s2.id, EdgeType.DEPENDS_ON)  # S1 依赖 S2
        llm = FakeLLM(reply_with([
            {"op": "add_edge", "src": "S2", "dst": "S1", "type": "depends_on"},  # 反向会成环
        ]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert res.ops_skipped
        assert set(eng.dependency_order(["S1", "S2"])) == {"S1", "S2"}  # 图仍无环

    def test_second_goal_is_skipped(self):
        eng = make_engine()  # 已有 G1
        llm = FakeLLM(reply_with([{"op": "add_node", "type": "goal", "content": "另一个目标"}]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert res.ops_skipped
        assert len([n for n in eng.nodes.values() if n.type == NodeType.GOAL]) == 1

    def test_duplicate_active_node_is_skipped(self):
        eng = make_engine()
        eng.add_fact("Lemma 2 给出鞅版本界")
        llm = FakeLLM(reply_with([{"op": "add_node", "type": "fact", "content": "Lemma 2 给出鞅版本界"}]))
        res = LLMCarryOverExtractor(llm).extract(RAW, eng)
        assert res.ops_skipped and not res.ops_applied
        assert len([n for n in eng.nodes.values() if n.type == NodeType.FACT]) == 1


class TestPrompt:
    def test_prompt_carries_graph_state_raw_and_next_question(self):
        eng = make_engine()
        llm = FakeLLM(reply_with([]))
        LLMCarryOverExtractor(llm).extract(RAW, eng, next_question="下一题：证明引理 3")
        prompt = llm.prompts[0]
        assert "[S1]" in prompt and "证明论文主定理" in prompt
        assert "Judge: CORRECT" in prompt
        assert "下一题：证明引理 3" in prompt

    def test_callable_protocol_returns_result(self):
        eng = make_engine()
        extractor = LLMCarryOverExtractor(FakeLLM(reply_with([], summary="摘要")))
        res = extractor(RAW, eng)  # CarryOverManager 以 (raw, engine) 调用
        assert isinstance(res, ExtractionResult)
        assert res.summary == "摘要"
