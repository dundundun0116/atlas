import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerPanel } from "../AnswerPanel";
import type { ResearchResult } from "../../api/client";

const base: ResearchResult = {
  answer: "Edmund Bellinger",
  evidence: [],
  confidence: 0.84,
  retrieval_rounds: 2,
  stop_reason: "answered",
  trace_id: "t-1",
  failure: "",
} as ResearchResult;

describe("AnswerPanel", () => {
  it("渲染答案、置信度与停止原因", () => {
    render(<AnswerPanel result={base} />);
    expect(screen.getByText("Edmund Bellinger")).toBeInTheDocument();
    expect(screen.getByText("84%")).toBeInTheDocument();
    expect(screen.getByText("answered")).toBeInTheDocument();
    expect(screen.getByText("完成")).toBeInTheDocument();
  });

  it("失败时显示失败类别而不是“完成”", () => {
    render(<AnswerPanel result={{ ...base, answer: "unknown", failure: "no_evidence" }} />);
    expect(screen.getByText("no_evidence")).toBeInTheDocument();
    expect(screen.getByText(/没有足够证据/)).toBeInTheDocument();
  });

  it("没有结果时不渲染面板", () => {
    const { container } = render(<AnswerPanel result={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
