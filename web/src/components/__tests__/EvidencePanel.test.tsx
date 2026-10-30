import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvidencePanel } from "../EvidencePanel";

describe("EvidencePanel", () => {
  it("展示来源、检索器、排名与引用片段", () => {
    render(
      <EvidencePanel
        evidence={[
          { source: "Wooden Heart (novel)", chunk_id: "c1", quote: "…written by Martin", score: 0.91, retriever: "kg", rank: 2 },
          { source: "Beta", chunk_id: "c2", quote: "beta text", score: 0.5, retriever: "bm25", rank: 1 },
        ]}
      />,
    );
    expect(screen.getByText("证据（2）")).toBeInTheDocument();
    expect(screen.getByText("Wooden Heart (novel)")).toBeInTheDocument();
    expect(screen.getByText("rank 2")).toBeInTheDocument();
    expect(screen.getByText("score 0.910")).toBeInTheDocument();
    expect(screen.getByText("…written by Martin")).toBeInTheDocument();
  });

  it("空证据给出提示", () => {
    render(<EvidencePanel evidence={[]} />);
    expect(screen.getByText("没有返回证据")).toBeInTheDocument();
  });
});
