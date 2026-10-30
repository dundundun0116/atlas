#!/usr/bin/env python3
"""NIM 冒烟测试。

用法：
  python scripts/smoke_llm.py --list-models   # 列出端点可用模型（deepseek 会标记）
  python scripts/smoke_llm.py                 # 跑 1 次 chat + 1 次 embed，打印遥测汇总
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.llm.client import LLMClient
from compassrag.llm.telemetry import summarize


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-models", action="store_true",
                    help="只列出端点可用模型，不发起 chat/embed")
    args = ap.parse_args()

    client = LLMClient()

    if args.list_models:
        for mid in client.list_models():
            mark = "   <-- deepseek" if "deepseek" in mid.lower() else ""
            print(f"{mid}{mark}")
        return

    r = client.chat([{"role": "user", "content": "用一句话介绍你自己。"}], tag="smoke", max_tokens=512)
    print(f"chat ok: {r.text!r}")
    print(f"  model={r.model} in={r.prompt_tokens} out={r.completion_tokens} "
          f"latency={r.latency_ms:.0f}ms")

    import os
    if os.environ.get("EMBEDDING_MODEL"):
        vecs = client.embed(["CompassRAG 是一个面向多跳问答的检索系统。",
                             "Adaptive retrieval decides where to search and when to stop."],
                            tag="smoke")
        print(f"embed ok: {len(vecs)} vectors, dim={len(vecs[0])}")
    else:
        print("embed 跳过：EMBEDDING_MODEL 未配置（本项目走本地 BGE-M3，见 scripts/embed_corpus.py）")

    print("\n遥测汇总:")
    for s in summarize(client.telemetry.path):
        print(f"  {s['kind']:<6} {s['tag']:<8} {s['model']:<44} "
              f"calls={s['calls']} tokens={s['total_tokens']} avg={s['avg_latency_ms']}ms")


if __name__ == "__main__":
    main()
