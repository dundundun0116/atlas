"""opencode go 兼容层：给 OpenAI 客户端注入会话标识头。

opencode go 端点要求客户端标识自身并携带稳定的会话 ID：
  - x-opencode-session: <每会话稳定 ID>（路由与前缀缓存优化）
  - User-Agent: 自有标识（而非通用 SDK 名）
参考 https://opencode.ai/docs/go/#where-can-i-use-it

经 PYTHONPATH 在解释器启动时自动加载；仅在 OPENCODE_COMPAT=1 时生效，
不影响本机其他 python 进程（如 pytest）。
"""

import os

if os.getenv("OPENCODE_COMPAT") == "1":
    try:
        import openai

        _orig_init = openai.OpenAI.__init__

        def _patched_init(self, *args, **kwargs):
            headers = dict(kwargs.pop("default_headers", None) or {})
            headers.setdefault(
                "x-opencode-session", os.getenv("OPENCODE_SESSION", "atlas-eval")
            )
            headers.setdefault("User-Agent", os.getenv("OPENCODE_UA", "atlas-eval/0.1"))
            kwargs["default_headers"] = headers
            _orig_init(self, *args, **kwargs)

        openai.OpenAI.__init__ = _patched_init
    except Exception as exc:  # pragma: no cover - 补丁失败不阻塞主流程
        import sys

        print(f"[opencode_compat] patch skipped: {exc}", file=sys.stderr)
