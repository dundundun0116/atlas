"""Atlas CLI：当前提供图状态快照导出，demo 子命令后续添加。

用法：
    python -m atlas.cli export runs/graphs/<user_id>.jsonl --out snapshot.html
"""

import argparse

from atlas.export import export_graph


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas 工具集（图状态记忆）")
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export", help="把图状态 JSONL 导出为 SVG/HTML 快照")
    export_cmd.add_argument("graph", help="图状态 JSONL 路径（引擎 save_jsonl 产物）")
    export_cmd.add_argument("--out", required=True, help="输出路径（.svg 或 .html）")

    args = parser.parse_args(argv)
    if args.command == "export":
        path = export_graph(args.graph, args.out)
        print(f"已导出：{path}")
        return 0
    parser.error(f"未知子命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
