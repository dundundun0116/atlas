"""openai 兼容端点的统一客户端：chat + embed，逐调用遥测。

限流退避交给 openai SDK 的 max_retries（configs/models.yaml request.max_retries），
只遥测最终结果，不重复造轮子。
"""

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI

from .telemetry import Telemetry

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    finish_reason: str = ""
    reasoning_tokens: int = 0


class LLMError(RuntimeError):
    pass


def _make_http_client(timeout: float):
    """强制 IPv4 出口。

    某些网络环境下 DNS 会先返回一个 Teredo（2001::/32）AAAA 地址，httpx 选中它后
    TCP 一直停在 SYN_SENT，表现为"跑评测没输出、CPU 0%"。把本地出口绑到 0.0.0.0
    就只走 IPv4。`LLM_FORCE_IPV4=0` 可关闭。
    """
    if os.environ.get("LLM_FORCE_IPV4", "1") == "0":
        return None
    try:
        import httpx
        return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"),
                            timeout=timeout)
    except Exception:  # noqa: BLE001 - 拿不到就直接用默认客户端
        return None


def batch_texts(texts: list, batch_size: int) -> list[list]:
    return [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]


class LLMClient:
    def __init__(self, repo_root=None, telemetry_path=None, session_id=None):
        self.root = Path(repo_root) if repo_root else REPO_ROOT
        load_dotenv(self.root / ".env")
        cfg_path = self.root / "configs" / "models.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {} if cfg_path.exists() else {}
        req = cfg.get("request", {})

        base = os.environ.get("LLM_BASE_URL")
        key = os.environ.get("LLM_API_KEY")
        model = os.environ.get("LLM_MODEL")
        if not (base and key and model):
            raise LLMError("缺少 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL——请复制 .env.example 为 .env 并填入真实值")
        self.model = model

        # opencode Go 订阅要求客户端带会话头（路由与 prompt 缓存优化）并自报身份
        self.session_id = session_id or os.environ.get("OPENCODE_SESSION_ID") or uuid.uuid4().hex[:16]
        default_headers = {
            "User-Agent": "CompassRAG/0.1.0",
            "x-opencode-session": self.session_id,
        }

        timeout = float(req.get("timeout_s", 120))
        retries = int(req.get("max_retries", 4))
        self._client = OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=retries,
                              default_headers=default_headers,
                              http_client=_make_http_client(timeout))

        e_base = os.environ.get("EMBEDDING_BASE_URL") or base
        e_key = os.environ.get("EMBEDDING_API_KEY") or key
        self.embedding_model = os.environ.get("EMBEDDING_MODEL") or ""  # 不回退主模型：embed 端点与 chat 不同族
        self._embed_client = self._client if (e_base == base and e_key == key) else \
            OpenAI(base_url=e_base, api_key=e_key, timeout=timeout, max_retries=retries,
                   default_headers=default_headers,
                   http_client=_make_http_client(timeout))
        self._batch_size = int(cfg.get("embedding", {}).get("batch_size", 96))

        tel_path = telemetry_path or self.root / cfg.get("telemetry", {}).get("path", "runs/telemetry.jsonl")
        self.telemetry = Telemetry(tel_path)

    def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None,
             thinking: bool | None = None) -> LLMResponse:
        """一次对话调用；messages 为 openai 格式 [{"role": ..., "content": ...}]。

        thinking=False 显式关闭思维链（网关实测形状 {"thinking": {"type": "disabled"}}，
        其余参数形状会被静默忽略）——机械任务（抽取/NER）省 token 且不被推理打转拖垮。
        """
        model = model or self.model
        start = time.perf_counter()
        kwargs = {"max_tokens": max_tokens} if max_tokens else {}
        if thinking is False:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            resp = self._client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, **kwargs)
        except Exception as e:
            self.telemetry.record(kind="chat", tag=tag, model=model,
                                  latency_ms=(time.perf_counter() - start) * 1000, error=e)
            raise
        latency = (time.perf_counter() - start) * 1000
        usage = resp.usage
        choice = resp.choices[0]
        finish_reason = getattr(choice, "finish_reason", None) or ""
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None) if details else None
        row = self.telemetry.record(
            kind="chat", tag=tag, model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
            latency_ms=latency,
            finish_reason=finish_reason,
            reasoning_tokens=reasoning_tokens)
        return LLMResponse(
            text=choice.message.content or "",
            model=model,
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            latency_ms=latency,
            finish_reason=finish_reason,
            reasoning_tokens=row.get("reasoning_tokens", 0) or 0)

    def embed(self, texts, *, tag="embed") -> list[list[float]]:
        """批量向量化；按 batch_size 分批，一次遥测行聚合全部用量。"""
        if not self.embedding_model:
            raise LLMError("未配置 EMBEDDING_MODEL——本地 BGE-M3 兜底尚未接入")
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        vectors = []
        prompt_tokens = 0
        total_tokens = 0
        start = time.perf_counter()
        try:
            for batch in batch_texts(texts, self._batch_size):
                resp = self._embed_client.embeddings.create(model=self.embedding_model, input=batch)
                vectors.extend(d.embedding for d in resp.data)
                usage = getattr(resp, "usage", None)
                if usage:
                    prompt_tokens += usage.prompt_tokens or 0
                    total_tokens += usage.total_tokens or 0
        except Exception as e:
            self.telemetry.record(kind="embed", tag=tag, model=self.embedding_model,
                                  prompt_tokens=prompt_tokens, total_tokens=total_tokens,
                                  latency_ms=(time.perf_counter() - start) * 1000,
                                  n_items=len(texts), error=e)
            raise
        latency = (time.perf_counter() - start) * 1000
        self.telemetry.record(kind="embed", tag=tag, model=self.embedding_model,
                              prompt_tokens=prompt_tokens, total_tokens=total_tokens,
                              latency_ms=latency, n_items=len(texts))
        return vectors

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._client.models.list().data)
