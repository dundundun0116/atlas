"""单篇冒烟：用指定 config 跑一篇论文（默认最小篇），验证接入层真实链路（少量、不产生正式对比数字）。

- 默认论文 2308.12401（4 子任务，全库最小之一）——只验证链路与提取质量；
- `SMOKE_SUBTASKS=<K>` 只跑该篇前 K 个子任务（更省的"跑几个"验证）；
- 结果写到 `runs/json/math_smoke/`（与正式结果目录隔离，不污染后续全量跑分；
  正式跑分用 run_math.sh 的抽样子集，届时该篇会照常重跑）；
- 提取器/组装的审计行打到 `runs/memory_server.log`（[atlas][carry_log] 等）。

用法：SMOKE_PAPER=2308.12401 SMOKE_SUBTASKS=2 eval/run_math.sh [config.json]
"""

import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "external", "MemoryArena"))

import run_math  # noqa: E402
from datasets import load_dataset  # noqa: E402

_SNAPSHOT_GLOB = os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--ZexueHe--memoryarena/snapshots/*/{config}/data.jsonl"
)
_SMOKE_OUTPUT_DIR = os.path.join(ROOT, "runs", "json", "math_smoke")


def _load_local(config: str):
    files = sorted(glob.glob(_SNAPSHOT_GLOB.format(config=config)))
    if not files:
        raise SystemExit(f"[smoke] 本地缓存未命中：{_SNAPSHOT_GLOB.format(config=config)}")
    print(f"[smoke] 本地缓存直载：{files[0]}", flush=True)
    return load_dataset("json", data_files=files[0], split="train")


def main() -> None:
    cfg_path = sys.argv[1]
    paper_name = sys.argv[2] if len(sys.argv) > 2 else "2308.12401"
    cfg = json.load(open(cfg_path))
    cfg["output"]["json_output_dir"] = _SMOKE_OUTPUT_DIR  # 与正式结果隔离
    if (cfg.get("agent") or {}).get("orchestration"):
        from atlas_agent import maybe_enable_orchestration  # eval/ 在 sys.path（脚本目录）

        maybe_enable_orchestration(cfg, run_math)  # config 一行开关

    ds = _load_local(cfg["task_specific"]["dataset"]["hf_config"])
    row = next((r for r in ds if r["paper_name"] == paper_name), None)
    if row is None:
        raise SystemExit(f"[smoke] 未找到论文 {paper_name}")

    tasks = [
        (row["questions"][i], row["answers"][i], row["backgrounds"][i])
        for i in range(len(row["questions"]))
    ]
    limit = int(os.environ.get("SMOKE_SUBTASKS", "0") or 0)
    if limit > 0:
        tasks = tasks[:limit]
    print(
        f"[smoke] {paper_name}: {len(tasks)} 子任务；"
        f"memory={cfg['memory']['memory_system_name']}；config={cfg_path}",
        flush=True,
    )
    # 冒烟不做断点续跑：跑几个子任务时若按"已处理"跳过，会拿不到新结果
    run_math._is_paper_processed = lambda cfg_, paper_key, expected: False
    logs = run_math.run_task_with_memory_and_env(cfg=cfg, tasks=tasks, paper_key=paper_name)
    correct = sum(1 for log in logs if log.get("is_correct"))
    print(f"[smoke] 完成：{correct}/{len(logs)} 正确", flush=True)
    print(
        f"[smoke] 结果：{os.path.join(_SMOKE_OUTPUT_DIR, cfg['memory']['memory_system_name'], paper_name)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
