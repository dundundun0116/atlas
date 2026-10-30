"""Atlas CLI：图状态快照导出 + 统一任务报告。

用法：
    python -m atlas.cli export runs/graphs/<user_id>.jsonl --out snapshot.html
    python -m atlas.cli report <session_id> --db runs/graphs/atlas.db --out runs/reports/demo.html
"""

import argparse
import os

from atlas.export import export_graph


def _cmd_report(args) -> int:
    from atlas.report import write_task_report
    from atlas.runtime import AgentRuntime
    from atlas.storage.repository import GraphRepository, open_database

    database = open_database(args.db)
    try:
        repository = GraphRepository(database)
        runtime = AgentRuntime(repository)
        state = runtime.get_state(args.session)
        trace = runtime.get_trace(args.session)
        replay = runtime.replay(args.session)
        nodes = repository.nodes(args.session)
    finally:
        database.close()

    out = args.out or os.path.join("runs", "reports", f"{args.session}.html")
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    path = write_task_report(
        out, state=state, trace=trace, replay=replay, nodes=nodes,
        research_base_url=args.research_base_url, title=args.title,
    )
    print(f"已生成任务报告：{path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas 工具集（图状态记忆 / 任务报告）")
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export", help="把图状态 JSONL 导出为 SVG/HTML 快照")
    export_cmd.add_argument("graph", help="图状态 JSONL 路径（引擎 save_jsonl 产物）")
    export_cmd.add_argument("--out", required=True, help="输出路径（.svg 或 .html）")

    report_cmd = sub.add_parser("report", help="生成统一任务报告（状态 / 角色 / 证据 / 研究轨迹）")
    report_cmd.add_argument("session", help="会话（任务）id")
    report_cmd.add_argument("--db", required=True, help="Atlas repository 的 SQLite 路径")
    report_cmd.add_argument("--out", help="输出 HTML 路径（默认 runs/reports/<session>.html）")
    report_cmd.add_argument("--research-base-url", default="",
                            help="CompassRAG 服务地址，用于把 trace_id 变成可点链接")
    report_cmd.add_argument("--title", default="Atlas 任务报告")

    args = parser.parse_args(argv)
    if args.command == "export":
        path = export_graph(args.graph, args.out)
        print(f"已导出：{path}")
        return 0
    if args.command == "report":
        return _cmd_report(args)
    parser.error(f"未知子命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
