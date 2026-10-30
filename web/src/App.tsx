import { useCallback, useEffect, useRef, useState } from "react";
import {
  buildIndex,
  cancelRun,
  getHealth,
  getIndexStatus,
  getRun,
  listDocuments,
  listRuns,
  subscribeEvents,
  submitAsync,
  uploadDocument,
  type DocumentMeta,
  type HealthResponse,
  type IndexStatus,
  type ResearchResult,
  type RunSummary,
  type TraceEvent,
} from "./api/client";
import { appendEventUnique } from "./api/client";
import { ResearchInput, type RunOptions } from "./components/ResearchInput";
import { AnswerPanel } from "./components/AnswerPanel";
import { EvidencePanel } from "./components/EvidencePanel";
import { TraceTimeline } from "./components/TraceTimeline";
import { RunHistory } from "./components/RunHistory";
import { CorpusPanel } from "./components/CorpusPanel";

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [documents, setDocuments] = useState<DocumentMeta[]>([]);
  const closeRef = useRef<(() => void) | null>(null);

  const refreshCorpus = useCallback(async () => {
    try {
      const [status, docs] = await Promise.all([getIndexStatus(), listDocuments()]);
      setIndexStatus(status);
      setDocuments(docs);
    } catch {
      setIndexStatus(null);      // 后端没挂 ingest 时安静降级
      setDocuments([]);
    }
  }, []);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await listRuns());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    getHealth().then(setHealth).catch((e) => setError(String(e)));
    refreshRuns();
    refreshCorpus();
    return () => closeRef.current?.();
  }, [refreshCorpus, refreshRuns]);

  const openRun = useCallback(async (id: string) => {
    closeRef.current?.();
    setError(null);
    setEvents([]);
    setResult(null);
    setRunId(id);
    try {
      const envelope = await getRun(id);
      setResult(envelope.result ?? null);
      const history = await fetch(`/runs/${id}/events/json`).then((r) => (r.ok ? r.json() : []));
      setEvents(history as TraceEvent[]);
      setRunning((envelope.summary?.status ?? "") === "running");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  const start = useCallback(
    async (query: string, options: RunOptions) => {
      closeRef.current?.();
      setError(null);
      setEvents([]);
      setResult(null);
      setRunning(true);
      try {
        const summary = await submitAsync({ query, ...options, async_mode: false } as any);
        setRunId(summary.run_id);
        const refreshTerminal = () => {
          getRun(summary.run_id)
            .then((envelope) => {
              setResult(envelope.result ?? null);
              setRunning(false);
              refreshRuns();
            })
            .catch((e) => setError(String(e)));
        };
        closeRef.current = subscribeEvents(summary.run_id, (event) => {
          // 只把「轨迹步骤」放进时间线：status/finish 这类生命周期事件用于状态迁移，
          // 否则 finish 会出现两条（生命周期一条 + finish 节点一条）。
          const isStep = Boolean((event.payload as any)?.step_id);
          setEvents((prev) => (isStep ? appendEventUnique(prev, event) : prev));
          if (event.kind === "finish" || event.kind === "error") refreshTerminal();
        }, refreshTerminal);
      } catch (e) {
        setError(String(e));
        setRunning(false);
      }
    },
    [refreshRuns],
  );

  const stop = useCallback(async () => {
    if (!runId) return;
    try {
      await cancelRun(runId);
    } catch (e) {
      setError(String(e));
    }
  }, [runId]);

  return (
    <main className="app">
      <header>
        <h1>CompassRAG 研究工作台</h1>
        <span className="muted small">
          {health ? `模型 ${health.model || "?"} · 工具 ${(health.tools ?? []).join("/")}` : "连接中…"}
        </span>
      </header>

      {error && <div className="error">{error}</div>}

      <ResearchInput running={running} onSubmit={start} onCancel={stop} />

      <div className="columns">
        <div className="col">
          <AnswerPanel result={result} />
          {result && <EvidencePanel evidence={result.evidence} />}
        </div>
        <div className="col">
          <TraceTimeline events={events} />
          <RunHistory runs={runs} onOpen={openRun} onRefresh={refreshRuns} />
          <CorpusPanel
            status={indexStatus}
            documents={documents}
            onUpload={async (payload) => {
              const saved = await uploadDocument(payload);
              await refreshCorpus();
              return saved.duplicate
                ? `已存在同一文档（${saved.meta.doc_id}），未重复写入`
                : `已上传 ${saved.meta.name}（${saved.meta.doc_id}）`;
            }}
            onBuild={async () => {
              const version = await buildIndex();
              await refreshCorpus();
              const stages = (version.stages ?? []).map((s) => `${s.name}:${s.status}`).join(" · ");
              return `构建 ${version.version}：${version.n_chunks} 块（${stages}）`;
            }}
          />
        </div>
      </div>
    </main>
  );
}
