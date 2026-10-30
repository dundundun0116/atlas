#!/usr/bin/env python3
"""导出 OpenAPI 到 `web/openapi.json`，供前端生成 TypeScript 类型。

用**桩 Agent** 建 app（只是为了让 FastAPI 注册路由，不会调用模型/索引），
所以这个脚本在没配 .env、没建索引的机器上也能跑。
"""

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from compassrag.agent import Agent, AgentConfig
from compassrag.api.server import create_app


class _StubTool:
    chunk_ids: list[str] = []

    def search(self, query, k=10):  # pragma: no cover - 不会被执行
        return []


class _StubLLM:
    model = "openapi-export"
    embedding_model = ""

    def chat(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


def main() -> int:
    app = create_app(Agent(_StubLLM(), {"stub": _StubTool()}, {},
                           AgentConfig(use_dense=False)),
                     ingest_root=Path(tempfile.mkdtemp(prefix="openapi-ingest-")))
    out = REPO_ROOT / "web" / "openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
