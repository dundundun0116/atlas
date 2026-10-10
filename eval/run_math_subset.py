"""math 域 long_context 基线的抽样子集入口（决策 #7：每域固定 n、固定种子）。

用法：eval/run_math.sh            # 默认 n=10, seed=20261010
      SUBSET_N=20 eval/run_math.sh
实现方式：monkeypatch 官方 runner 的 load_dataset，返回固定随机子集；
子集清单写入 runs/subset_<config>_n<N>_seed<SEED>.txt 备查（同一清单用于
之后 Atlas 的对照跑，保证可比）。
多次重复（不同种子重复跑）按要求留给 M3 的 Atlas-vs-基线对比阶段。
"""

import glob
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "external", "MemoryArena"))

import run_math  # noqa: E402
from datasets import load_dataset as _original_load_dataset  # noqa: E402

N = int(os.environ.get("SUBSET_N", "10"))
SEED = int(os.environ.get("SUBSET_SEED", "20261010"))
# 指定全库索引（逗号分隔，如 SUBSET_INDICES="39,22,36"）时跳过随机抽样，按给定顺序跑——
# 用于小论文初步数字（B 路线）：先跑短的，尽早拿到一篇完整对照
SUBSET_INDICES = os.environ.get("SUBSET_INDICES", "").strip()

# 本地缓存快照优先：HF 直连偶发失败时 datasets 会走有 bug 的"缓存回退"路径
# （把 split 拼进 config 名导致 ValueError），本地直载完全绕开 hub。
_SNAPSHOT_GLOB = os.path.expanduser(
    "~/.cache/huggingface/hub/datasets--ZexueHe--memoryarena/snapshots/*/{config}/data.jsonl"
)


def _load_dataset_resilient(path, config, split):
    local_files = sorted(glob.glob(_SNAPSHOT_GLOB.format(config=config)))
    if local_files:
        print(f"[subset] 本地缓存直载：{local_files[0]}")
        return _original_load_dataset("json", data_files=local_files[0], split="train")
    print("[subset] 本地缓存未命中，走 HF Hub 在线加载")
    return _original_load_dataset(path, config, split)


def _subset_loader(path, config, split):
    ds = _load_dataset_resilient(path, config, split)
    if SUBSET_INDICES:
        idx = [int(x) for x in SUBSET_INDICES.split(",") if x.strip()]
        bad = [i for i in idx if not 0 <= i < len(ds)]
        if bad:
            raise SystemExit(f"[subset] 索引越界 {bad}（全库 {len(ds)} 篇）")
        tag = "idx" + "-".join(str(i) for i in idx)
    else:
        n = min(N, len(ds))
        idx = sorted(random.Random(SEED).sample(range(len(ds)), n))
        tag = f"n{n}_seed{SEED}"
    names = [ds[i]["paper_name"] for i in idx]
    print(f"[subset] {config}: 全量 {len(ds)} -> 按索引 {idx if SUBSET_INDICES else f'抽样 n={N} seed={SEED}'}")
    for i, name in zip(idx, names):
        print(f"[subset]   #{i:2d}: {name} ({len(ds[i]['questions'])} 子任务)")
    manifest = os.path.join(ROOT, "runs", f"subset_{config}_{tag}.txt")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with open(manifest, "w", encoding="utf-8") as f:
        f.write("\n".join(f"{i}\t{name}" for i, name in zip(idx, names)) + "\n")
    return ds.select(idx)


run_math.load_dataset = _subset_loader

if __name__ == "__main__":
    cfg_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(ROOT, "eval", "configs", "math_longcontext.json")
    )
    cfg = json.load(open(cfg_path))
    if (cfg.get("agent") or {}).get("orchestration"):
        from atlas_agent import maybe_enable_orchestration  # eval/ 在 sys.path（脚本目录）

        maybe_enable_orchestration(cfg, run_math)  # 决策 #17：config 一行开关
    run_math.main(cfg)
