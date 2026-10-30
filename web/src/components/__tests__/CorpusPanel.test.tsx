import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CorpusPanel } from "../CorpusPanel";
import type { IndexStatus } from "../../api/client";

const status = (over: Partial<IndexStatus> = {}): IndexStatus => ({
  runtime_version: "v1",
  built_version: "v2",
  mismatch: true,
  n_documents: 3,
  n_chunks: 42,
  n_versions: 2,
  last_error: "",
  available_tools: ["bm25"],
  stage_status: { bm25: "ok", dense: "skipped" },
  ...over,
}) as IndexStatus;

describe("CorpusPanel", () => {
  it("展示文档数/块数/版本，并在版本不一致时告警", () => {
    render(<CorpusPanel status={status()} documents={[]} onUpload={vi.fn()} onBuild={vi.fn()} />);
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText(/运行时索引与最新构建不是同一版/)).toBeInTheDocument();
  });

  it("上传失败时把错误显示出来而不是静默", async () => {
    const onUpload = vi.fn().mockRejectedValue(new Error("422 bad"));
    render(<CorpusPanel status={status()} documents={[]} onUpload={onUpload} onBuild={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("文档内容"), { target: { value: "hello" } });
    fireEvent.click(screen.getByText("上传文档"));
    await waitFor(() => expect(screen.getByText(/422 bad/)).toBeInTheDocument());
  });
});
