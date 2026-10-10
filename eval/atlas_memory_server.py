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
# 提取器模型：实测 deepseek-v4.1-flash 在长记录段上推理失控（8000 token 全被推理吃光、
# 单次调用 17 分钟且常以连接错误告终）；mimo-v2.6-flash 同任务 33s、推理 ~400 token、
# JSON 合规且提取合理（2026-10-10 对比 deepseek-flash / mimo / v4.1-flash 三款）。
EXTRACTOR_MODEL = os.getenv("ATLAS_EXTRACTOR_MODEL", "mimo-v2.6-flash")
# 推理型模型推理 token 计入 max_tokens，太小会导致正文为空；8000 留足余量。
EXTRACTOR_MAX_TOKENS = int(os.getenv("ATLAS_EXTRACTOR_MAX_TOKENS", "8000"))
# 图状态快照目录（决策 #10）：每次 wrap 落盘 <user_id>.jsonl，供 CLI 导出 HTML/SVG 与事后核查；
# 设为空串可关闭。
GRAPH_DUMP_DIR = os.getenv("ATLAS_GRAPH_DUMP_DIR", os.path.join(ROOT, "runs", "graphs"))


def count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text, disallowed_special=()))


class OpenAIChatLLM:
    """提取器 LLM（opencode go 通道；兼容层自动注入会话头）。

    连接类错误重试：经系统代理（Clash）的长连接池会偶发失效
    （2026-10-10 冒烟实测 APIConnectionError），换新客户端重试，避免白白降级。
    """

    def __init__(self):
        self.client = self._new_client()
        self.model = EXTRACTOR_MODEL

    @staticmethod
    def _new_client():
        from openai import OpenAI

        return OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )

    def __call__(self, prompt: str) -> str:
        import openai

        last_error = None
        for _ in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.0,
                    max_tokens=EXTRACTOR_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                last_error = e
                self.client = self._new_client()  # 丢弃旧连接池再试
        else:
            raise last_error
        choice = response.choices[0]
        content = choice.message.content or ""
        if not content:
            # 空正文几乎总是"推理占满 max_tokens"：把诊断信息留在服务日志里
            usage = response.usage
            reasoning = None
            if usage is not None and usage.completion_tokens_details is not None:
                reasoning = usage.completion_tokens_details.reasoning_tokens
            print(
                f"[atlas][extractor] 空正文：finish={choice.finish_reason} reasoning={reasoning} "
                f"completion={getattr(usage, 'completion_tokens', None)} 提示词={len(prompt)}字符",
                flush=True,
            )
        return content


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
        dump_path = (
            os.path.join(GRAPH_DUMP_DIR, f"{req.user_id}.jsonl") if GRAPH_DUMP_DIR else None
        )
        SYSTEMS[req.user_id] = AtlasMemorySystem(
            req.user_id,
            llm=OpenAIChatLLM(),
            token_counter=count_tokens,
            budget_tokens=BUDGET_TOKENS,
            dump_path=dump_path,
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
    for attr in ("carry_log", "extract_errors", "assemble_errors", "dump_errors"):
        events = getattr(system, attr, None)
        if not events:
            continue
        for message in events:
            print(f"[atlas][{attr}] {message}", flush=True)
        events.clear()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
