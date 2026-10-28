import type { ResearchResult } from "../api/client";

export function AnswerPanel(props: { result: ResearchResult | null }) {
  if (!props.result) return null;
  const { answer, stop_reason, failure, confidence, retrieval_rounds } = props.result;
  const refused = (answer ?? "").trim().toLowerCase() === "unknown" || !answer;
  return (
    <section className="panel answer-panel">
      <h2>答案</h2>
      <p className={refused ? "answer muted" : "answer"}>{answer || "（无答案）"}</p>
      <dl className="meta">
        <div>
          <dt>置信度</dt>
          <dd>{Math.round((confidence ?? 0) * 100)}%</dd>
        </div>
        <div>
          <dt>停止原因</dt>
          <dd>{stop_reason || "—"}</dd>
        </div>
        <div>
          <dt>检索轮数</dt>
          <dd>{retrieval_rounds ?? 0}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>
            {failure ? <span className="badge bad">{failure}</span> : <span className="badge ok">完成</span>}
          </dd>
        </div>
      </dl>
      {refused && <p className="hint">没有足够证据时系统会拒答，而不是编造。</p>}
    </section>
  );
}
