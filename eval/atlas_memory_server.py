"""Atlas 记忆服务：一个端口按 memory_system_name 分派（long_context / atlas）。

- long_context：复用官方 LongContextMemorySystem（与 lite_memory_server.py 相同逻辑）；
- atlas：AtlasMemorySystem（图状态记忆 + 结转 + 四区组装；决策 #11–#14），
  提取器走 openai 客户端（OPENAI_API_KEY/OPENAI_BASE_URL；opencode 兼容层经 PYTHONPATH 生效）。

接口与官方 memory/server.py 一致（/memory/initialize|add|wrap_user_prompt），
因此 M3 的 A/B 只差 config 里 memory_system_name 一行。
提取器模型默认 deepseek-v4.1-flash（可用 ATLAS_EXTRACTOR_MODEL 覆盖），
预算默认 8000 tokens（决策 #13，可用 ATLAS_BUDGET_TOKENS 覆盖）。
"""

import importlib.util
import os
import sys
from typing import Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import tiktoken
import uvicorn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from atlas.service import AtlasMemorySystem  # noqa: E402

# 按文件路径加载官方 long_context，绕开 memory_systems/__init__.py 的重型 eager import
_LONG_CONTEXT_FILE = os.path.join(
    ROOT, "external", "MemoryArena", "memory", "memory_systems", "long_context.py"
)
_spec = importlib.util.spec_from_file_location("arena_long_context", _LONG_CONTEXT_FILE)
_arena_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_arena_lc)
LongContextMemorySystem = _arena_lc.LongContextMemorySystem

_TOKENIZER = tiktoken.encoding_for_model("gpt-4o-mini")
BUDGET_TOKENS = int(os.getenv("ATLAS_BUDGET_TOKENS", "8000"))
EXTRACTOR_MODEL = os.getenv("ATLAS_EXTRACTOR_MODEL", "deepseek-v4.1-flash")


def count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text, disallowed_special=()))


class OpenAIChatLLM:
    """提取器 LLM（opencode go 通道；兼容层自动注入会话头）。"""

    def __init__(self):
        from openai import OpenAI

        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )
        self.model = EXTRACTOR_MODEL

    def __call__(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""


class InitializeRequest(BaseModel):
    user_id: str
    memory_system_name: str


class AddRequest(BaseModel):
    user_id: str
    chunk: str
    memory_system_name: str


class WrapRequest(BaseModel):
    user_id: str
    question: str
    memory_system_name: str


app = FastAPI(title="Atlas Memory Server (long_context + atlas)")
SYSTEMS: Dict[str, object] = {}


def _get(user_id: str):
    system = SYSTEMS.get(user_id)
    if system is None:
        raise HTTPException(status_code=404, detail="User not initialized")
    return system


@app.post("/memory/initialize")
def initialize(req: InitializeRequest):
    if req.memory_system_name == "long_context":
        SYSTEMS[req.user_id] = LongContextMemorySystem(user_id=req.user_id)
    elif req.memory_system_name == "atlas":
        SYSTEMS[req.user_id] = AtlasMemorySystem(
            req.user_id,
            llm=OpenAIChatLLM(),
            token_counter=count_tokens,
            budget_tokens=BUDGET_TOKENS,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported memory_system_name: {req.memory_system_name}",
        )
    return {"status": "ok"}


@app.post("/memory/add")
def add(req: AddRequest):
    _get(req.user_id).add_chunk(req.chunk)
    return {"status": "ok"}


@app.post("/memory/wrap_user_prompt")
def wrap(req: WrapRequest):
    system = _get(req.user_id)
    prompt = system.wrap_user_prompt(req.question)
    _flush_events(system)
    return {"prompt": prompt}


def _flush_events(system) -> None:
    """把结转审计行与降级记录打到服务日志（跑分不中断，但过程必须可审计）。"""
    for attr in ("carry_log", "extract_errors", "assemble_errors"):
        events = getattr(system, attr, None)
        if not events:
            continue
        for message in events:
            print(f"[atlas][{attr}] {message}", flush=True)
        events.clear()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
