import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TraceTimeline } from "../TraceTimeline";
import { appendEventUnique, type TraceEvent } from "../../api/client";

const ev = (seq: number, kind: string, payload: Record<string, unknown> = {}): TraceEvent =>
  ({ run_id: "r1", seq, kind, ts: 0, payload }) as TraceEvent;

describe("TraceTimeline", () => {
  it("按顺序渲染事件并带 token/延迟标签", () => {
    render(
      <TraceTimeline
        events={[
          ev(0, "status", { status: "running" }),
          ev(1, "retrieve", { detail: { query: "who wrote it", tools: [{ tool: "bm25" }] }, tokens: 120, latency_ms: 35 }),
          ev(2, "answer", { tokens: 300 }),
          ev(3, "end"),
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);              // end 事件不渲染
    expect(items[1]).toHaveTextContent("检索（bm25）：who wrote it");
    expect(items[1]).toHaveTextContent("120 tok");
    expect(items[1]).toHaveTextContent("35 ms");
  });

  it("没有事件时显示占位", () => {
    render(<TraceTimeline events={[]} />);
    expect(screen.getByText("还没有事件")).toBeInTheDocument();
  });

  it("重复 seq 事件只保留一条", () => {
    const first = ev(1, "retrieve");
    expect(appendEventUnique([first], { ...first })).toHaveLength(1);
  });
});
