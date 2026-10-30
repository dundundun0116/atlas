import type { RunSummary } from "../api/client";

const STATUS_LABEL: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
};

export function RunHistory(props: {
  runs: RunSummary[];
  onOpen: (runId: string) => void;
  onRefresh: () => void;
}) {
  return (
    <section className="panel history-panel">
      <div className="panel-head">
        <h2>历史运行</h2>
        <button className="ghost" onClick={props.onRefresh}>
          刷新
        </button>
      </div>
      {props.runs.length === 0 && <p className="muted">还没有运行</p>}
      <ul>
        {props.runs.map((run) => (
          <li key={run.run_id}>
            <button className="link" onClick={() => props.onOpen(run.run_id)}>
              <span className="question">{run.question}</span>
              <span className={`badge ${run.status === "completed" ? "ok" : run.status === "failed" ? "bad" : ""}`}>
                {STATUS_LABEL[run.status] ?? run.status}
              </span>
            </button>
            <div className="muted small">
              {run.answer ? `答：${run.answer.slice(0, 40)}` : "（未完成）"}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
