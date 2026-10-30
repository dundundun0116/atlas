"""Atlas 编排 agent：把多智能体编排挂到官方 MathAgent 上。

- runner 里的 MathAgent 由入口脚本 monkeypatch 替换（不改官方代码，见
  maybe_enable_orchestration）；编排开关 = config 的 `agent.orchestration` 一行；
- 协调全经共享图：角色经 memory server 的 /memory/state_view 与 /memory/role_note 读写；
- 失败逐级降级：编排 → 单发 Worker 式解答 → 官方 MathAgent.act（保证跑分不中断）。
"""

import os
import sys
from typing import Optional

# 让 eval venv 能导入仓库根的 atlas 包（入口脚本会 cd 到 external/，不保证根在 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from agent.math import MathAgent  # noqa: E402
from atlas.orchestration import WORKER_FALLBACK_PROMPT, Orchestrator, TaskStateMachine  # noqa: E402

WORKER_MAX_TOKENS = int(os.getenv("ATLAS_WORKER_MAX_TOKENS", "24000"))  # 解题（含推理）预算
JSON_MAX_TOKENS = int(os.getenv("ATLAS_ROLE_JSON_MAX_TOKENS", "4000"))  # 机械性 JSON 调用预算


class MemoryServerGraphClient:
    """共享图客户端（HTTP；协调全经图）。"""

    def __init__(self, base_url: str, user_id: str, timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout

    def state_view(self, role: str) -> str:
        resp = requests.post(
            f"{self.base_url}/memory/state_view",
            json={"user_id": self.user_id, "role": role}, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()["view"]

    def known_ids(self) -> set:
        resp = requests.post(
            f"{self.base_url}/memory/known_ids",
            json={"user_id": self.user_id}, timeout=self.timeout,
        )
        resp.raise_for_status()
        return set(resp.json().get("ids") or [])

    def role_note(self, role: str, kind: str, content: str, reason: str = "",
                  evidence: str = "") -> dict:
        resp = requests.post(
            f"{self.base_url}/memory/role_note",
            json={"user_id": self.user_id, "role": role, "kind": kind, "content": content,
                  "reason": reason, "evidence": evidence},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


def research_tool_from_env():
    """阶段 A：ATLAS_RESEARCH_URL 指向 CompassRAG 时才启用研究工具；否则纯 LLM 流程。"""
    url = os.environ.get("ATLAS_RESEARCH_URL", "").strip()
    if not url:
        return None
    try:
        from atlas.tools import HttpCompassRAGTool

        timeout_s = float(os.environ.get("ATLAS_RESEARCH_TIMEOUT", "30"))
        print(f"[orchestration] 研究工具已启用：{url}", flush=True)
        return HttpCompassRAGTool(url, timeout_s=timeout_s)
    except Exception as e:
        print(f"[orchestration] 研究工具初始化失败，退回纯 LLM：{type(e).__name__}: {e}", flush=True)
        return None


class OrchestratedMathAgent(MathAgent):
    """编排形态：act() 走 Planner → Worker → Auditor →（必要时）Worker 修订。

    推理开关：机械性 JSON 调用（Planner/Auditor）走关推理通道（opencode go 端点支持
    `thinking.type=disabled`，实测推理 token 归零、快一个数量级）；真正解题的 Worker/修订
    保持推理（与单 agent 臂同等能力）。
    """

    _NO_THINK_BODY = {"thinking": {"type": "disabled"}}

    def __init__(self, *args, memory_url: Optional[str] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_url = memory_url or os.getenv("ATLAS_MEMORY_URL", "http://127.0.0.1:8000")

    def _llm(self, prompt: str, think: bool = True) -> str:
        # 推理型模型需要留足 max_tokens（官方后端默认 2000 会被推理吃光）
        kwargs = {} if think else {"extra_body": self._NO_THINK_BODY}
        return self.llm_backend.chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=WORKER_MAX_TOKENS if think else JSON_MAX_TOKENS,
            **kwargs,
        )

    def _solve_llm(self, prompt: str) -> str:
        """Worker/修订通道：带推理解；空正文（推理吃满预算）则关推理重试一次，保证有答案。"""
        text = self._llm(prompt, think=True)
        if not str(text or "").strip():
            print("[orchestration] worker 空正文（推理吃满预算），关推理重试一次", flush=True)
            text = self._llm(prompt, think=False)
        return text

    def act(self, prompt: str):
        problem = prompt.split("\nUser: ", 1)[1] if "\nUser: " in prompt else prompt
        graph = MemoryServerGraphClient(
            self.memory_url, user_id=str(getattr(self, "user_id", "") or "session")
        )
        trace = None
        self._subtask_seq = getattr(self, "_subtask_seq", 0) + 1
        subtask_id = f"S{self._subtask_seq}"
        # 阶段 5：每个子任务一个显式状态机，终态随 trace 落盘（可审计、可回放）
        machine = TaskStateMachine()
        try:
            orchestrator = Orchestrator(
                self._solve_llm,
                graph,
                llm_no_think=lambda p: self._llm(p, think=False),
                machine=machine,
                research_tool=self._research_tool,
            )
            answer, trace = orchestrator.solve(
                problem,
                session_id=str(getattr(self, "user_id", "") or "session"),
                subtask_id=subtask_id,
            )
        except Exception as e:
            print(f"[orchestration] 编排失败，降级单发：{type(e).__name__}: {e}", flush=True)
            try:
                answer = self._solve_llm(WORKER_FALLBACK_PROMPT.format(problem=problem)).strip()
            except Exception as e2:
                print(f"[orchestration] 单发失败，退回官方 act：{type(e2).__name__}: {e2}", flush=True)
                return super().act(prompt)
        if not str(answer or "").strip():
            return super().act(prompt)
        calls = list(trace.calls) if trace is not None else ["single_shot"]
        # tool_info 保持紧凑（完整角色输出在图里）：只记调用序列，供提取器与事后审计
        trace_info = [{
            "tool": "orchestration",
            "calls": calls,
            "task_state": machine.state.value,
            "research": {
                "trace_id": trace.research_trace_id if trace is not None else "",
                "rounds": trace.research_rounds if trace is not None else 0,
                "stop_reason": trace.research_stop_reason if trace is not None else "",
                "confidence": trace.research_confidence if trace is not None else 0.0,
                "error": trace.research_error if trace is not None else None,
                "fact_ids": list(trace.fact_ids) if trace is not None else [],
            },
        }]
        return {
            "type": "final",
            "answer": answer,
            "tool_trace": trace_info,
            "tool_info": trace_info,
            "input": prompt,
        }


def maybe_enable_orchestration(cfg: dict, run_math_module) -> bool:
    """config 一行开关：agent.orchestration=true 时替换 runner 的 MathAgent。"""
    if not (cfg.get("agent") or {}).get("orchestration"):
        return False
    run_math_module.MathAgent = OrchestratedMathAgent
    print("[orchestration] 已启用多智能体编排（Planner → Worker → Auditor）", flush=True)
    return True
