#!/usr/bin/env python3
"""起 Atlas 任务 API（统一工作台 / 演示用）。

    python scripts/serve_atlas_api.py --port 8001            # 空库，任务通过 API 创建
    python scripts/serve_atlas_api.py --port 8001 --demo     # 顺带建一个演示任务

前端 `web/` 的 `/atlas` 页面通过 Vite 代理访问本服务（见 web/vite.config.ts）。
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas.api.routes import create_app  # noqa: E402
from atlas.runtime import AgentRuntime  # noqa: E402
from atlas.storage.repository import GraphRepository, open_database  # noqa: E402


def build_llm():
    """按 .env / 环境变量装配一个 OpenAI 兼容的编排模型；没配就返回 None。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # pragma: no cover - .env 可选
        pass
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=os.getenv("LLM_BASE_URL") or None)
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    def llm(prompt: str) -> str:
        response = client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""

    return llm


def seed_demo(repository: GraphRepository, session: str = "demo") -> None:
    """建一个可看的演示任务：创建 → 规划推进 → 写一条决策与一条研究事实。"""
    if repository.get_session(session) is not None:
        return
    from atlas.service import ServiceRuntime
    import json

    runtime = AgentRuntime(repository)
    runtime.create_task(session, "回答：爱因斯坦出生于哪一年？", idempotency_key="demo-create")
    runtime.advance_task(session, "plan_ready", reason="先检索再作答",
                         idempotency_key="demo-plan")
    service = ServiceRuntime(repository, session)
    service.note("planner", "先确认出生年份，再作答", kind="decision", reason="事实型问题")
    service.note(
        "research", "1879 年", kind="fact",
        evidence=json.dumps([{
            "source": "Albert Einstein", "chunk_id": "wiki:einstein",
            "quote": "阿尔伯特·爱因斯坦，1879 年 3 月 14 日出生于德国乌尔姆。",
            "score": 0.93, "retriever": "bm25", "rank": 1,
        }], ensure_ascii=False),
        source="Albert Einstein", confidence=0.8,
        meta={"research_trace_id": "demo-trace-0001", "research_stop_reason": "answered",
              "research_rounds": 1, "research_confidence": 0.8},
    )
    print(f"[atlas-api] 已建演示任务：{session}")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "runs" / "atlas_api" / "atlas.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--demo", action="store_true", help="建一个演示任务")
    parser.add_argument("--compassrag-url", default="",
                        help="CompassRAG 服务地址；配置后工作台可对任务发起检索")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    database = open_database(args.db)
    repository = GraphRepository(database)
    if args.demo:
        seed_demo(repository)
    orchestrate_llm = build_llm()
    if orchestrate_llm is None:
        print("[atlas-api] 未配置 LLM_API_KEY：/orchestrate 返回 unrecoverable（可看/可检索，不可编排）")
    else:
        print(f"[atlas-api] 编排模型已启用：{os.getenv('LLM_MODEL', 'gpt-4o-mini')}")

    research_tool = None
    if args.compassrag_url:
        from atlas.tools import HttpCompassRAGTool

        research_tool = HttpCompassRAGTool(args.compassrag_url)
        print(f"[atlas-api] ResearchTool 已启用：{args.compassrag_url}")
    else:
        print("[atlas-api] 未配置 --compassrag-url：/research 端点返回 unrecoverable（工作台可看不可发起检索）")
    app = create_app(repository, research_tool=research_tool,
                     orchestrate_llm=orchestrate_llm)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
