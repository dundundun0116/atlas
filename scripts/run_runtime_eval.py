"""Task 6：长程运行时评测入口（两种模式必须分开看）。

- `--mode offline`：脚本化研究者。用来验证指标口径、故障注入与 replay 是否自洽，
  **不是真实模型成绩**（见 decisions/D9）。
- `--mode real`：真实模型研究者。需要 `OPENAI_BASE_URL` / `OPENAI_API_KEY`
  与 `--model`（或环境变量 `ATLAS_EVAL_MODEL`）。只有这一模式的结果可以写进简历。

用法：
    python scripts/run_runtime_eval.py --mode offline
    python scripts/run_runtime_eval.py --mode real --model deepseek-v4.1-flash --repeats 3
"""

import argparse
import json
import os
import sys
import urllib.request
from typing import Callable, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from atlas.evaluation import (  # noqa: E402
    ModelResearcher,
    build_policies,
    grouped_metrics,
    load_fixtures,
    render_table,
    run_matrix,
    save_run,
)


def openai_compatible_llm(model: Optional[str] = None,
                          temperature: float = 0.0,
                          timeout_s: float = 120.0) -> Callable[[str], str]:
    """最小 OpenAI 兼容客户端（不引依赖，只用标准库）。"""
    base = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not base or not key:
        raise SystemExit(
            "real 模式需要 OPENAI_BASE_URL 与 OPENAI_API_KEY（可写进 eval/.env 后 source）"
        )
    resolved_model = model or os.environ.get("ATLAS_EVAL_MODEL", "deepseek-v4.1-flash")

    def call(prompt: str) -> str:
        body = json.dumps(
            {
                "model": resolved_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            # 自带 UA：部分兼容端点会拒绝通用 SDK 标识
            "User-Agent": os.environ.get("ATLAS_EVAL_UA", "atlas-runtime-eval/1.0"),
        }
        if os.environ.get("OPENCODE_COMPAT") == "1":
            headers["x-opencode-session"] = os.environ.get(
                "OPENCODE_SESSION", "atlas-runtime-eval"
            )
        request = urllib.request.Request(
            f"{base}/chat/completions", data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"] or ""

    return call


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atlas 长程运行时评测")
    parser.add_argument("--mode", choices=("offline", "real"), default="offline")
    parser.add_argument("--model", default="", help="real 模式使用的模型名")
    parser.add_argument("--repeats", type=int, default=1, help="real 模式重复次数（简历数字必须>1）")
    parser.add_argument("--out", default="", help="结果目录（默认 runs/runtime_eval_<mode>）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    fixtures = load_fixtures()
    policies = build_policies()
    config = {
        "mode": args.mode,
        "model": args.model or os.environ.get("ATLAS_EVAL_MODEL", ""),
        "fixtures": [f.family for f in fixtures],
        "policies": sorted(policies),
        "repeats": args.repeats,
        "honesty_note": (
            "offline = 脚本化研究者，仅验证指标口径；不得作为简历效果数字"
            if args.mode == "offline"
            else "real = 真实模型结果，需注明样本量/预算/重复次数"
        ),
    }

    runs = []
    if args.mode == "offline":
        runs = run_matrix(fixtures, policies)
    else:
        llm = openai_compatible_llm(args.model or None)
        factory = lambda fixture: ModelResearcher(fixture, llm)  # noqa: E731
        for _ in range(max(1, args.repeats)):
            runs.extend(run_matrix(fixtures, policies, researcher_factory=factory))

    config["n_runs"] = len(runs)
    out = args.out or os.path.join("runs", f"runtime_eval_{args.mode}")
    path = save_run(out, config=config, runs=runs)
    print(render_table(grouped_metrics(runs, "policy"), f"按记忆策略（mode={args.mode}）"))
    print(f"\n结果包：{path}")
    print(f"口径：{config['honesty_note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
