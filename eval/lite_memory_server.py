"""轻量 memory server：只为 long_context 基线服务。

复用 MemoryArena 官方的 LongContextMemorySystem（逻辑零改动），
只暴露 MemoryClient 调用的三个端点，避免安装 mirix/mem0/letta 等重型依赖。
接口与官方 memory/server.py 保持一致（路径、字段、响应格式）。
"""

import importlib.util
import os
from typing import Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# 按文件路径加载，绕开 memory_systems/__init__.py 的重型 eager import
_LONG_CONTEXT_FILE = os.path.join(
    os.path.dirname(__file__), "..", "external", "MemoryArena", "memory", "memory_systems", "long_context.py"
)
_spec = importlib.util.spec_from_file_location("arena_long_context", os.path.abspath(_LONG_CONTEXT_FILE))
_arena_lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_arena_lc)
LongContextMemorySystem = _arena_lc.LongContextMemorySystem


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


app = FastAPI(title="Atlas Lite Memory Server (long_context)")
SYSTEMS: Dict[str, LongContextMemorySystem] = {}


def _get(user_id: str) -> LongContextMemorySystem:
    system = SYSTEMS.get(user_id)
    if system is None:
        raise HTTPException(status_code=404, detail="User not initialized")
    return system


@app.post("/memory/initialize")
def initialize(req: InitializeRequest):
    if req.memory_system_name != "long_context":
        raise HTTPException(status_code=400, detail=f"lite server only supports long_context, got {req.memory_system_name}")
    SYSTEMS[req.user_id] = LongContextMemorySystem(user_id=req.user_id)
    return {"status": "ok"}


@app.post("/memory/add")
def add(req: AddRequest):
    _get(req.user_id).add_chunk(req.chunk)
    return {"status": "ok"}


@app.post("/memory/wrap_user_prompt")
def wrap(req: WrapRequest):
    return {"prompt": _get(req.user_id).wrap_user_prompt(req.question)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
