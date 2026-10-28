"""Task 7 快速开始：不需要任何密钥，30 行看完整运行时能力。

跑法：`python examples/quickstart.py`
它演示：建任务 → 推进 → 暂停 → 换连接恢复 → 回放（含事件损坏检查）。
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from atlas.runtime import AgentRuntime, RuntimeFailure  # noqa: E402
from atlas.storage.repository import GraphRepository, open_database  # noqa: E402


def demo(db_path: str) -> dict:
    database = open_database(db_path)
    runtime = AgentRuntime(GraphRepository(database))
    runtime.create_task("quickstart", "把一件事做完", request_id="r0", idempotency_key="c0")
    runtime.advance_task("quickstart", "plan_ready", reason="路线已定", idempotency_key="p0")
    runtime.pause_task("quickstart", reason="等外部输入", idempotency_key="z0")
    paused = runtime.get_state("quickstart")["state"]
    database.close()

    database2 = open_database(db_path)  # 等价于进程重启
    runtime2 = AgentRuntime(GraphRepository(database2))
    resumed = runtime2.resume_task("quickstart", request_id="r1", idempotency_key="r1")
    state = runtime2.get_state("quickstart")
    replay = runtime2.replay("quickstart")
    database2.close()
    return {"paused": paused, "resumed": resumed["state"], "state": state["state"],
            "replay_ok": replay["ok"], "events": len(replay["timeline"])}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = demo(os.path.join(tmp, "quickstart.db"))
    print("暂停状态:", result["paused"])
    print("恢复后:", result["resumed"], "→ 当前:", result["state"])
    print("回放:", "OK" if result["replay_ok"] else "检测到损坏",
          "| 业务事件数:", result["events"])
    print("\n下一步：examples/research_agent.py 看完整研究链路（规划—研究—执行—核验—修订—恢复）")


if __name__ == "__main__":
    main()
