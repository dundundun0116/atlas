"""Atlas 记忆服务：一个端口按 memory_system_name 分派（long_context / atlas）。

- long_context：复用官方 LongContextMemorySystem（与 lite_memory_server.py 相同逻辑）；
- atlas：AtlasMemorySystem（图状态记忆 + 结转 + 四区组装），
  提取器走 openai 客户端（OPENAI_API_KEY/OPENAI_BASE_URL；opencode 兼容层经 PYTHONPATH 生效）。

接口与官方 memory/server.py 一致（/memory/initialize|add|wrap_user_prompt），
因此 M3 的 A/B 只差 config 里 memory_system_name 一行。
提取器模型默认 deepseek-v4.1-flash（可用 ATLAS_EXTRACTOR_MODEL 覆盖），
预算默认 8000 tokens。
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
from atlas.mage_baseline import MAGELiteMemorySystem  # noqa: E402
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
# 提取是机械性抽取任务，不需要长推理：opencode go 端点支持 thinking.type=disabled
# （实测推理 token 归零、单次从 17 分钟降到秒级）。设 ATLAS_EXTRACTOR_THINKING=on 可恢复。
EXTRACTOR_NO_THINK = os.getenv("ATLAS_EXTRACTOR_THINKING", "off").strip().lower() != "on"
# 图状态快照目录：每次 wrap 落盘 <user_id>.jsonl，供 CLI 导出 HTML/SVG 与事后核查；
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

        extra = {"extra_body": {"thinking": {"type": "disabled"}}} if EXTRACTOR_NO_THINK else {}
        last_error = None
        for _ in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.0,
                    max_tokens=EXTRACTOR_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                    **extra,
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


class StateViewRequest(BaseModel):
    user_id: str
    role: str = "worker"


class KnownIdsRequest(BaseModel):
    user_id: str


class RoleNoteRequest(BaseModel):
    user_id: str
    role: str
    kind: str
    content: str
    reason: str = ""
    evidence: str = ""


app = FastAPI(title="Atlas Memory Server (long_context + atlas)")
SYSTEMS: Dict[str, object] = {}


# ---------- 旁路镜像（阶段 E）：ATLAS_DB_PATH 设置时把内存图同步进库 ----------

DB_PATH = os.environ.get("ATLAS_DB_PATH", "").strip()
_REPOSITORY = None
MIRROR_ERRORS: list = []


def _repository():
    global _REPOSITORY
    if not DB_PATH:
        return None
    if _REPOSITORY is None:
        from atlas.storage.repository import GraphRepository, open_database

        _REPOSITORY = GraphRepository(open_database(DB_PATH))
    return _REPOSITORY


def _mirror(system) -> None:
    """评测逻辑不动：镜像失败只记日志，绝不打断跑分。"""
    repo = _repository()
    engine = getattr(system, "engine", None)
    if repo is None or engine is None:
        return
    session_id = getattr(system, "user_id", "unknown")
    try:
        if repo.get_session(session_id) is None:
            repo.create_session(session_id, goal="(evaluation session)")
        repo.import_engine(session_id, engine)
    except Exception as e:  # noqa: BLE001
        MIRROR_ERRORS.append(f"{type(e).__name__}: {e}")


def _get(user_id: str):
    system = SYSTEMS.get(user_id)
    if system is None:
        raise HTTPException(status_code=404, detail="User not initialized")
    return system


@app.post("/memory/initialize")
def initialize(req: InitializeRequest):
    if req.memory_system_name == "long_context":
        SYSTEMS[req.user_id] = LongContextMemorySystem(user_id=req.user_id)
    elif req.memory_system_name in ("atlas", "atlas_orch"):
        # atlas_orch：同一 AtlasMemorySystem，仅用于让编排臂的结果落到独立目录
        # （runner 用 memory_system_name 命名输出目录）；编排开关在 config 的 agent.orchestration
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
    elif req.memory_system_name == "mage":
        # MAGE 简化基线：叙事摘要记忆（C=子任务边界 LLM 摘要累计，R=空），
        # 与 Atlas 同边界结转、同 LLM，对照"叙事摘要 vs 结构化图"这一假设
        SYSTEMS[req.user_id] = MAGELiteMemorySystem(
            req.user_id, llm=OpenAIChatLLM(), token_counter=count_tokens,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported memory_system_name: {req.memory_system_name}",
        )
    return {"status": "ok"}


@app.post("/memory/add")
def add(req: AddRequest):
    system = _get(req.user_id)
    system.add_chunk(req.chunk)
    _mirror(system)
    return {"status": "ok"}


@app.post("/memory/wrap_user_prompt")
def wrap(req: WrapRequest):
    system = _get(req.user_id)
    prompt = system.wrap_user_prompt(req.question)
    _mirror(system)
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


def _current_atlas(user_id: str) -> AtlasMemorySystem:
    """按请求会话路由，避免多个任务共享最近初始化的图。"""
    system = SYSTEMS.get(user_id)
    if not isinstance(system, AtlasMemorySystem):
        raise HTTPException(status_code=404, detail="atlas session not initialized")
    return system


@app.post("/memory/state_view")
def state_view(req: StateViewRequest):
    """编排角色读共享图：返回计划区 + 踩坑提示。"""
    return {"view": _current_atlas(req.user_id).state_view(req.role)}


@app.post("/memory/known_ids")
def known_ids(req: KnownIdsRequest):
    """真实节点 id 集合：编排器据此强制 Auditor 引用校验（D10 债务收口）。"""
    return {"ids": sorted(_current_atlas(req.user_id).known_ids())}


@app.post("/memory/role_note")
def role_note(req: RoleNoteRequest):
    """编排角色写共享图：结论/疑虑落成节点，actor 标记角色。"""
    try:
        system = _current_atlas(req.user_id)
        note = system.role_note(req.role, req.kind, req.content, req.reason, req.evidence)
        _mirror(system)
        return note
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    # 默认 8000；跑分进行中改代码时可用 ATLAS_SERVER_PORT 另起实例（如 8002），不动主服务
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("ATLAS_SERVER_PORT", "8000")))
