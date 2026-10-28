import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelRun,
  getHealth,
  getRun,
  listRuns,
  subscribeEvents,
  submitAsync,
  type HealthResponse,
  type ResearchResult,
  type RunSummary,
  type TraceEvent,
} from "./api/client";
import { ResearchInput, type RunOptions } from "./components/ResearchInput";
import { AnswerPanel } from "./components/AnswerPanel";
import { EvidencePanel } from "./components/EvidencePanel";
import { TraceTimeline } from "./components/TraceTimeline";
import { RunHistory } from "./components/RunHistory";

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<(() => void) | null>(null);

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
    return () => closeRef.current?.();
  }, [refreshRuns]);

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
        closeRef.current = subscribeEvents(summary.run_id, (event) => {
          setEvents((prev) => (event.kind === "end" ? prev : [...prev, event]));
          if (event.kind === "finish" || event.kind === "error") {
            getRun(summary.run_id)
              .then((envelope) => {
                setResult(envelope.result ?? null);
                setRunning(false);
                refreshRuns();
              })
              .catch((e) => setError(String(e)));
          }
        });
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
          <EvidencePanel evidence={result?.evidence} />
        </div>
        <div className="col">
          <TraceTimeline events={events} />
          <RunHistory runs={runs} onOpen={openRun} onRefresh={refreshRuns} />
        </div>
      </div>
    </main>
  );
}
