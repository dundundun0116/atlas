"""阶段 2 验收：真实 HTTP 进程跑通 ResearchTool，并在**服务重启**后不重复写事实。

与 `test_research_tool.py` 的假 opener 不同，这里真的起一个 uvicorn 子进程：

    Atlas ──HttpCompassRAGTool──▶ POST /research ──▶ CompassRAG create_app(agent)

覆盖三件事：

1. 本地/HTTP 同一个 `ResearchTool` 契约：答案、证据、trace_id 都能拿到；
2. CompassRAG 服务被杀掉并重启后，同一次逻辑调用仍能完成（幂等键不变）；
3. Atlas 侧：PAUSED 任务在服务重启后从 checkpoint 恢复，且 repository 的幂等键阻止
   "同一子任务结论写两次"——重复写返回原节点，不产生第二个 Fact。
"""

import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from atlas.runtime import AgentRuntime
from atlas.service import ServiceRuntime
from atlas.storage.repository import GraphRepository, open_database
from atlas.tools import HttpCompassRAGTool, ResearchRequest

ROOT = Path(__file__).resolve().parents[1]

STUB_APP = textwrap.dedent('''
    """测试用 CompassRAG 服务：真实 app 工厂 + 假 LLM/检索器，零密钥。"""

    from compassrag.agent import Agent, AgentConfig
    from compassrag.api.server import create_app
    from compassrag.llm.client import LLMResponse

    STORE = {
        "c1": {"chunk_id": "c1", "title": "Alpha", "text": "Alpha text."},
        "c2": {"chunk_id": "c2", "title": "Beta", "text": "Beta text."},
    }


    class FakeTool:
        def search(self, query, k=10):
            return [("c1", 1.0), ("c2", 0.5)][:k]


    class FakeLLM:
        def chat(self, messages, *, tag="chat", temperature=0.0, max_tokens=None, model=None):
            return LLMResponse(text="Paris", model="fake", prompt_tokens=10,
                               completion_tokens=2, total_tokens=12, latency_ms=1.0,
                               finish_reason="stop")


    app = create_app(Agent(FakeLLM(), {"bm25": FakeTool()}, STORE,
                           AgentConfig(top_k=2)))
''')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


class StubServer:
    """uvicorn 子进程的最小控制器：start / stop / restart。"""

    def __init__(self, tmp_path: Path, timeout_s: float = 30.0):
        self.app_file = tmp_path / "stub_app.py"
        self.app_file.write_text(STUB_APP, encoding="utf-8")
        self.port = _free_port()
        self.timeout_s = timeout_s
        self.proc = None
        self.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "stub_app:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=str(self.app_file.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("CompassRAG 测试服务启动失败（进程提前退出）")
            if _healthy(self.port):
                return
            time.sleep(0.1)
        raise RuntimeError("CompassRAG 测试服务在超时内没有就绪")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def restart(self) -> None:
        self.stop()
        self.start()


@pytest.fixture
def server(tmp_path):
    srv = StubServer(tmp_path)
    try:
        yield srv
    finally:
        srv.stop()


def _request():
    return ResearchRequest(session_id="s1", subtask_id="S1", query="Where is Alpha?")


class TestHttpRoundtrip:
    def test_http_adapter_gets_answer_evidence_and_trace(self, server):
        tool = HttpCompassRAGTool(server.url)
        result = tool.research(_request())

        assert result.answer == "Paris"
        assert result.evidence and result.evidence[0].chunk_id == "c1"
        assert result.evidence[0].source == "Alpha"
        assert result.evidence[0].retriever == "bm25"
        assert result.evidence[0].rank == 1
        assert result.stop_reason == "answered"
        assert result.trace_id, "HTTP 契约必须把 CompassRAG trace_id 带回来"

    def test_same_port_survives_restart(self, server):
        first = HttpCompassRAGTool(server.url).research(_request())
        server.restart()
        second = HttpCompassRAGTool(server.url).research(_request())
        assert first.answer == second.answer == "Paris"
        # 重启后是新进程/新内存缓存：trace_id 会变，但契约字段仍完整
        assert second.trace_id and second.evidence


class TestRestartWithAtlasRuntime:
    def test_paused_task_resumes_and_fact_is_not_duplicated(self, server, tmp_path):
        database = open_database(tmp_path / "merged.db")
        repository = GraphRepository(database)
        runtime = AgentRuntime(repository)
        try:
            runtime.create_task("s1", "回答 Alpha 的所在地", idempotency_key="create-1")
            runtime.advance_task("s1", "plan_ready", reason="先检索再作答",
                                 idempotency_key="plan-1")
            runtime.pause_task("s1", reason="CompassRAG 服务需要重启",
                               idempotency_key="pause-1")

            tool = HttpCompassRAGTool(server.url)
            service = ServiceRuntime(repository, "s1")
            request = ResearchRequest(session_id="s1", subtask_id="S1",
                                      query="Where is Alpha?")
            first = tool.research(request)
            service.note("research", first.answer, kind="fact",
                         evidence=first.evidence_json(),
                         source=first.evidence[0].source,
                         idempotency_key="s1:S1:research")
            nodes_before = len(repository.nodes("s1"))

            # —— 服务被杀掉并重启 ——
            server.restart()

            second = tool.research(request)  # 同一次逻辑调用，幂等键不变
            import json
            assert json.loads(second.evidence_json()) == json.loads(first.evidence_json())
            service.note("research", second.answer, kind="fact",
                         evidence=second.evidence_json(),
                         source=second.evidence[0].source,
                         idempotency_key="s1:S1:research")  # 幂等键相同：返回原节点

            assert len(repository.nodes("s1")) == nodes_before, "重启后不得重复写 Fact"
            facts = [n for n in repository.nodes("s1") if n.type == "fact"]
            assert len(facts) == 1

            resumed = runtime.resume_task("s1", idempotency_key="resume-1")
            assert resumed["state"] == "executing"
        finally:
            server.stop()
            database.close()
