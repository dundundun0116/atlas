"""合并后的最小闭环（离线、零密钥）：一条命令看两层如何接线。

    Atlas 编排（Planner → Worker → Auditor）
        │  ResearchTool 协议
        ▼
    CompassRAG（路由 → 检索 → 证据 → 答案 + trace）
        │  证据回写
        ▼
    Atlas 共享图（Fact + provenance，可下钻 CompassRAG trace）

两步各用一套确定性替身，但**走的是真实代码路径**：

- CompassRAG 侧：真实 `Agent`（LangGraph 控制流 + 统一工具协议），假 LLM / 假检索器；
- Atlas 侧：真实 `Orchestrator` + `ServiceRuntime` + SQLite 仓库，脚本化 Planner/Auditor。

运行：`.venv/bin/python examples/atlas_compassrag_demo.py`
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from atlas.orchestration import Orchestrator  # noqa: E402
from atlas.service import ServiceRuntime  # noqa: E402
from atlas.storage.repository import GraphRepository, open_database  # noqa: E402
from atlas.tools import CompassRAGAgentTool, ResearchRequest, ResearchToolError  # noqa: E402
from compassrag.agent import Agent as CompassAgent  # noqa: E402
from compassrag.agent import AgentConfig  # noqa: E402
from compassrag.llm.client import LLMResponse  # noqa: E402

# --------------------------------------------------------------------------- #
# CompassRAG 侧替身：极小语料 + 关键词检索 + 固定答案
# --------------------------------------------------------------------------- #

CHUNKS = {
    "wiki:einstein": {
        "chunk_id": "wiki:einstein",
        "title": "Albert Einstein",
        "text": "阿尔伯特·爱因斯坦，1879 年 3 月 14 日出生于德国乌尔姆。",
    },
    "wiki:relativity": {
        "chunk_id": "wiki:relativity",
        "title": "狭义相对论",
        "text": "狭义相对论由爱因斯坦于 1905 年提出，描述惯性系中的时空结构。",
    },
}


class KeywordRetriever:
    """最小 BM25 替身：按关键词命中数排序，返回 (chunk_id, score)。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.queries = []

    def search(self, query, k=10):
        self.queries.append(query)
        hits = []
        for cid, chunk in self.chunks.items():
            overlap = sum(1 for token in ("爱因斯坦", "出生", "1879") if token in query
                          and token in chunk["text"])
            if overlap or "爱因斯坦" in query and "爱因斯坦" in chunk["text"]:
                hits.append((cid, float(overlap or 1)))
        return hits[:k]


class ScriptedLLM:
    """CompassRAG 的 LLM 替身：按队列回文本（answer 调用消耗一条）。"""

    def __init__(self, *texts):
        self.queue = list(texts)
        self.calls = 0

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
        self.calls += 1
        text = self.queue.pop(0) if self.queue else "1879 年"
        return LLMResponse(text=text, model="scripted", prompt_tokens=20,
                           completion_tokens=5, total_tokens=25, latency_ms=1.0,
                           finish_reason="stop")


def build_research_tool():
    """真实 CompassRAG Agent → Atlas 协议的进程内桥接。"""
    agent = CompassAgent(
        ScriptedLLM("1879 年"),
        {"bm25": KeywordRetriever(CHUNKS)},
        CHUNKS,
        AgentConfig(top_k=2),
    )
    return CompassRAGAgentTool(agent)


# --------------------------------------------------------------------------- #
# Atlas 侧：把 Orchestrator 接到真实仓库（共享图）
# --------------------------------------------------------------------------- #


class RepositoryGraph:
    """Orchestrator 需要的最小图接口，全部落真实仓库，不另建内存真源。"""

    def __init__(self, repository: GraphRepository, session_id: str):
        self.repository = repository
        self.session_id = session_id
        self.service = ServiceRuntime(repository, session_id)

    def state_view(self, role: str) -> str:
        nodes = self.repository.nodes(self.session_id)
        active = [n for n in nodes if n.status == "active"]
        lines = [f"[{role}] 活跃节点 {len(active)} 个"]
        for node in active[-6:]:
            lines.append(f"- {node.type}: {str(node.content)[:80]}")
        return "\n".join(lines)

    def role_note(self, role, kind, content, reason="", evidence=""):
        row = self.service.note(role, content, kind=kind, reason=reason, evidence=evidence)
        return {"node_id": row.node_id, "kind": kind, "actor": role}


PLAN = json.dumps({"route": "先确认人物出生年份，再作答", "risks": "需要可定位来源",
                   "reason": "题目是事实型问题"})


class AtlasLLM:
    """Atlas 编排的脚本化 LLM：Planner / Worker / Auditor 各一条。"""

    def __init__(self):
        self.replies = [
            PLAN,
            "爱因斯坦出生于 1879 年（依据：Albert Einstein 条目）。",
            json.dumps({"verdict": "ok", "issues": ""}),
        ]

    def __call__(self, prompt: str) -> str:
        return self.replies.pop(0)


# --------------------------------------------------------------------------- #


def run(db_path: str) -> dict:
    tool = build_research_tool()
    database = open_database(db_path)
    repository = GraphRepository(database)
    session = "demo-merged"
    repository.create_session(session, goal="回答：爱因斯坦出生于哪一年？")
    graph = RepositoryGraph(repository, session)

    # ---- A. 直接看一次 CompassRAG 研究请求（含证据与 trace） ----
    request = ResearchRequest(session_id=session, subtask_id="S1",
                              query="爱因斯坦出生于哪一年？")
    research = tool.research(request)
    trace = tool.get_trace(research.trace_id)

    # ---- B. Atlas 编排：规划 → 研究取证 → 草稿 → 核验 ----
    orchestrator = Orchestrator(AtlasLLM(), graph, research_tool=tool)
    answer, orchestration_trace = orchestrator.solve(
        "回答：爱因斯坦出生于哪一年？", session_id=session, subtask_id="S1",
        research_query="爱因斯坦出生于哪一年？")

    facts = [n for n in repository.nodes(session) if n.type == "fact" and n.actor == "research"]
    database.close()
    return {
        "research": research,
        "compass_trace": trace,
        "answer": answer,
        "atlas_trace": orchestration_trace,
        "facts": facts,
    }


def failure_path() -> str:
    """无证据时：工具给 no_evidence，Atlas 不写 Fact —— 失败是可分类的，不是自然语言。"""
    tool = build_research_tool()
    try:
        tool.research(ResearchRequest(session_id="demo", subtask_id="S9",
                                      query="完全不相关的问题：如何用铁锅炼金？"))
    except ResearchToolError as exc:
        return f"{exc.kind}（{exc.message}）"
    return "unexpected-success"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = run(os.path.join(tmp, "merged.db"))

    research = out["research"]
    trace = out["compass_trace"]
    atlas_trace = out["atlas_trace"]

    print("== A. CompassRAG：单次研究请求 ==")
    print(f"  answer      : {research.answer}")
    print(f"  stop_reason : {research.stop_reason}   rounds={research.retrieval_rounds}"
          f"   confidence={research.confidence:.2f}")
    for item in research.evidence:
        print(f"  evidence    : {item.source} [{item.retriever} #{item.rank}] "
              f"{item.quote[:60]}…")
    print(f"  检索查询    : {[q['query'] for q in trace['queries']]}" if trace else "")

    print("\n== B. Atlas：编排 + 真实检索 ==")
    print(f"  final answer: {out['answer']}")
    print(f"  fact ids    : {[n.node_id for n in out['facts']]}")
    for fact in out["facts"]:
        evidence = json.loads(fact.evidence)
        print(f"  fact        : {fact.content}")
        print(f"   provenance  : {evidence[0]['source']} / chunk={evidence[0]['chunk_id']} "
              f"/ retriever={evidence[0]['retriever']}")

    print("\n== C. 两条 trace 对得上 ==")
    print(f"  Atlas research_trace_id : {atlas_trace.research_trace_id}")
    print(f"  CompassRAG trace_id     : {research.trace_id}")
    print(f"  一致                    : "
          f"{atlas_trace.research_trace_id == research.trace_id}")

    print("\n== D. 失败路径（无证据）==")
    print(f"  {failure_path()} —— 未写 Fact，任务状态不受影响")


if __name__ == "__main__":
    main()
