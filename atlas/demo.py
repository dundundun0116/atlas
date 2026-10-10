"""离线演示（决策 #10）：脚本化提取器跑一段迷你会话，展示图状态记忆 + 四区组装 + 快照导出。

不需要任何 API 凭据：提取器用脚本化替身（真实 LLM 提取器见 eval/atlas_memory_server.py 与
eval/atlas_agent.py）。演示场景用旅行规划（Goal/Subtask/Constraint/Decision/Artifact/Fact
六类节点都能自然出现，便于肉眼核对）。

用法：python -m atlas.demo [--out runs/demo/graph.html] [--budget 8000]
"""

import argparse
import json
import os
import sys
from typing import List

from atlas.export import graph_to_html, graph_to_svg
from atlas.service import AtlasMemorySystem

# 脚本化提取器：按结转顺序返回（真实实现由 LLM 产出同样形状的 JSON）
SCRIPTED_EXTRACTIONS: List[str] = [
    json.dumps({  # 结转 #1：日期确定
        "summary": "确定了出行日期（5 月 14-16 日），并确认高铁往返可用。",
        "operations": [
            {"op": "add_node", "type": "goal", "content": "规划杭州 3 日行（2 人，5 月中旬）"},
            {"op": "add_node", "type": "artifact", "content": "日期确定：5 月 14-16 日"},
        ],
    }, ensure_ascii=False),
    json.dumps({  # 结转 #2：酒店决定被推翻（决策变迁 + 作废）
        "summary": "查了西湖区酒店，展会期间房价翻倍，改住滨江区。",
        "operations": [
            {"op": "add_node", "type": "constraint", "content": "总预算 5000 元", "constrains": "S2"},
            {"op": "add_node", "type": "decision", "content": "住西湖区", "reason": "离景点近",
             "evidence": "地图距离"},
            {"op": "add_node", "type": "fact", "content": "5 月中旬杭州有展会，西湖区酒店均价 800 元/晚"},
            {"op": "supersede", "target": "D1", "content": "住滨江区", "reason": "展会期间房价翻倍，超预算"},
        ],
    }, ensure_ascii=False),
]

SESSIONS = [
    ("定日期", "5 月 14-16 日（周五-周日）", "CORRECT"),
    ("订酒店", "先查西湖区：均价 800 元/晚，超预算；改查滨江区：最低 400 元/晚", "CORRECT"),
    ("出行方式", "高铁往返：杭州东站有地铁 1 号线，到滨江区 30 分钟", "CORRECT"),
]


def _token_counter():
    try:
        import tiktoken

        encoder = tiktoken.encoding_for_model("gpt-4o-mini")
        return lambda text: len(encoder.encode(text, disallowed_special=()))
    except ImportError:  # 无 tiktoken 时用粗略估计，演示可跑
        return lambda text: max(1, len(text) // 4)


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, prompt: str) -> str:
        return self.replies.pop(0) if self.replies else "{}"


def run_demo(out_path: str, budget: int) -> None:
    system = AtlasMemorySystem(
        "demo", llm=ScriptedLLM(SCRIPTED_EXTRACTIONS), token_counter=_token_counter(),
        budget_tokens=budget,
    )
    print("=" * 78)
    print("Atlas 离线演示：图状态记忆 + 结转式上下文管理（提取器为脚本化替身）")
    print("=" * 78)
    system.add_chunk("Initial result: Empty\n")  # runner 在首个 wrap 前也会这么做

    last_context = ""
    for index, (task, solution, judge) in enumerate(SESSIONS):
        question = f"### PROBLEM:\n{task}"
        last_context = system.wrap_user_prompt(question)
        print(f"\n—— 子任务 {index + 1}：{task}")
        print(f"   组装上下文 {_token_counter()(last_context)} tokens；"
              f"图节点 {len(system.engine.nodes)}；摘要 {len(system.summaries)} 条")
        system.add_chunk(f"## Task: {task}\n## solution: {solution}\n## Judge: {judge}\n")

    print("\n" + "=" * 78)
    print("最后一个子任务实际看到的上下文（四区组装）")
    print("=" * 78)
    print(last_context)

    print("=" * 78)
    print(f"图状态：{len(system.engine.nodes)} 节点 / {len(system.engine.edges)} 边")
    for node in system.engine.nodes.values():
        print(f"  [{node.id}] {node.type.value}/{node.status.value}（{node.actor}）{node.content}")
    for edge in system.engine.edges:
        print(f"  {edge.src} -{edge.type.value}-> {edge.dst}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(graph_to_html(system.engine, title="Atlas 演示：杭州 3 日行"))
    svg_path = os.path.splitext(out_path)[0] + ".svg"
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(graph_to_svg(system.engine))
    print(f"\n快照已导出：{out_path}（HTML，含图例与节点表）与 {svg_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="atlas.demo", description="Atlas 离线演示")
    parser.add_argument("--out", default="runs/demo/graph.html", help="快照输出路径（.html）")
    parser.add_argument("--budget", type=int, default=8000, help="组装上下文预算（tokens）")
    args = parser.parse_args(argv)
    run_demo(args.out, args.budget)
    return 0


if __name__ == "__main__":
    sys.exit(main())
