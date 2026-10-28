import type { ResearchEvidence } from "../api/client";

export function EvidencePanel(props: { evidence: ResearchEvidence[] | undefined }) {
  const items = props.evidence ?? [];
  return (
    <section className="panel evidence-panel">
      <h2>证据（{items.length}）</h2>
      {items.length === 0 && <p className="muted">没有返回证据</p>}
      <ol>
        {items.map((item) => (
          <li key={item.chunk_id}>
            <div className="evidence-head">
              <strong>{item.source || item.chunk_id}</strong>
              <span className="tag">{item.retriever || "?"}</span>
              <span className="tag">rank {item.rank}</span>
              {typeof item.score === "number" && <span className="tag">score {item.score.toFixed(3)}</span>}
            </div>
            <blockquote>{item.quote}</blockquote>
          </li>
        ))}
      </ol>
    </section>
  );
}
