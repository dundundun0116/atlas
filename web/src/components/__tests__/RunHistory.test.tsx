import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RunHistory } from "../RunHistory";
import type { RunSummary } from "../../api/client";

const run = (id: string, status: string): RunSummary =>
  ({ run_id: id, question: `q-${id}`, status, answer: "Paris" }) as RunSummary;

describe("RunHistory", () => {
  it("列出运行并可打开历史（不触发新运行）", () => {
    const onOpen = vi.fn();
    render(<RunHistory runs={[run("a", "completed"), run("b", "cancelled")]} onOpen={onOpen} onRefresh={() => {}} />);
    expect(screen.getByText("q-a")).toBeInTheDocument();
    expect(screen.getByText("完成")).toBeInTheDocument();
    expect(screen.getByText("已取消")).toBeInTheDocument();
    fireEvent.click(screen.getByText("q-a"));
    expect(onOpen).toHaveBeenCalledWith("a");
  });
});
