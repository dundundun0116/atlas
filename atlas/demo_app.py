"""Atlas 演示页（决策 #10 的 streamlit 部分）：离线跑一段迷你会话，可视化图状态记忆与结转式上下文管理。

与 atlas.demo 同一套素材（脚本化提取器 + 旅行规划场景），零 API 凭据；真实 LLM 提取器见
eval/atlas_memory_server.py。侧栏预算滑块即时重跑组装，可观察预算对四区配比与结转摘要的影响。

用法：.venv/bin/streamlit run atlas/demo_app.py
"""

import base64
import sys
from typing import List

import streamlit as st

from atlas.demo import SCRIPTED_EXTRACTIONS, SESSIONS, ScriptedLLM, _token_counter
from atlas.export import graph_to_svg
from atlas.service import AtlasMemorySystem

st.set_page_config(page_title="Atlas 演示", page_icon="🗺️", layout="wide")

# ---------------------------------------------------------------------------
# 迷你会话：一段旅行规划，两次结转（提取器为脚本化替身，逻辑与真实链路一致）
# ---------------------------------------------------------------------------


def run_session(budget: int) -> AtlasMemorySystem:
    system = AtlasMemorySystem(
        "demo", llm=ScriptedLLM(SCRIPTED_EXTRACTIONS), token_counter=_token_counter(),
        budget_tokens=budget,
    )
    system.add_chunk("Initial result: Empty\n")  # runner 在首个 wrap 前也会这么做
    return system


budget = st.sidebar.slider("组装上下文预算（tokens）", 2000, 16000, 8000, step=500)
st.sidebar.markdown(
    "Atlas 的四区组装器按此预算动态适配各区；调低预算可观察近期原始记录（R 区）"
    "被压缩、图摘要（C 区）承担状态的情况。"
)
st.sidebar.divider()
st.sidebar.markdown(
    "**Atlas**：图状态记忆 + 结转式上下文管理 + 多智能体编排，"
    "面向 long-horizon 任务。演示为离线脚本化版本，评测接入见仓库 `eval/`。"
)

st.title("Atlas 演示：图状态记忆 + 结转式上下文管理")
st.caption(
    "场景：旅行规划迷你会话（3 个子任务）。每次子任务结束（wrap）触发结转——"
    "LLM 提取器把执行状态（结论 / 约束 / 决定 / 产出物）写入状态图，历史才能安全压缩。"
    "此处提取器为脚本化替身，输出与真实 LLM 同形状。"
)

system = run_session(budget)
context_counter = _token_counter()

# 逐子任务展示：wrap（组装上下文）→ 解题 → 记录入会话
for index, (task, solution, judge) in enumerate(SESSIONS):
    context = system.wrap_user_prompt(f"### PROBLEM:\n{task}")
    tokens = context_counter(context)
    with st.expander(f"子任务 {index + 1}：{task}", expanded=(index == len(SESSIONS) - 1)):
        st.markdown(f"**组装上下文：{tokens} tokens**（图节点 {len(system.engine.nodes)} 个，"
                    f"叙事摘要 {len(system.summaries)} 条）")
        st.code(context, language="markdown")
        st.markdown(f"**agent 产出：** {solution}")
        st.markdown(f"**judge 判分：** `{judge}`")
    system.add_chunk(f"## Task: {task}\n## solution: {solution}\n## Judge: {judge}\n")

st.divider()

# 图状态可视化：SVG 快照（data URL 内嵌）+ 节点/边表
left, right = st.columns([3, 2])
with left:
    st.subheader("图状态快照")
    svg = graph_to_svg(system.engine)
    data_url = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode()
    st.iframe(data_url, height=460)
with right:
    st.subheader("节点")
    st.dataframe(
        [{
            "id": node.id,
            "类型": node.type.value,
            "状态": node.status.value,
            "行动者": node.actor,
            "内容": node.content,
        } for node in system.engine.nodes.values()],
        width="stretch", hide_index=True,
    )
    st.subheader("边")
    st.dataframe(
        [{"src": edge.src, "类型": edge.type.value, "dst": edge.dst}
         for edge in system.engine.edges],
        width="stretch", hide_index=True,
    )

st.caption(
    "看点：D1「住西湖区」被 supersede 为「住滨江区」（决策变迁，作废而非删除，原因入图）；"
    "结转后每个子任务的上下文由图摘要 + 近期记录重新组装，而非依赖完整历史。"
)
