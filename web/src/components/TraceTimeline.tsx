import { describeEvent, type TraceEvent } from "../api/client";

export function TraceTimeline(props: { events: TraceEvent[] }) {
  const events = props.events.filter((e) => e.kind !== "end");
  return (
    <section className="panel trace-panel">
      <h2>过程（{events.length} 步）</h2>
      {events.length === 0 && <p className="muted">还没有事件</p>}
      <ol className="timeline">
        {events.map((event) => {
          const payload = (event.payload ?? {}) as Record<string, any>;
          return (
            <li key={`${event.seq}-${event.kind}`} className={`kind-${event.kind}`}>
              <span className="kind">{event.kind}</span>
              <span className="desc">{describeEvent(event)}</span>
              {typeof payload.tokens === "number" && payload.tokens > 0 && (
                <span className="tag">{payload.tokens} tok</span>
              )}
              {typeof payload.latency_ms === "number" && (
                <span className="tag">{payload.latency_ms} ms</span>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
