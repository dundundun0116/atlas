import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AtlasPage from "./AtlasPage";

const TASK = {
  session_id: "demo",
  goal: "回答：爱因斯坦出生于哪一年？",
  state: "executing",
  nodes: [
    {
      id: "D1", type: "decision", status: "active", content: "先检索再作答",
      actor: "planner", source: "", confidence: 0, valid_until: "",
      verified_at: "",
    },
    {
      id: "F1", type: "fact", status: "active", content: "1879 年",
      actor: "research", source: "Albert Einstein", confidence: 0.8,
      valid_until: "", verified_at: "",
    },
  ],
  edges: [],
  checkpoint: null,
  provenance: {},
};

const REPORT = { session_id: "demo", state: "executing", report: "# 任务报告\n状态: executing" };

function respond(body: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("/report") ? respond(REPORT) : respond(TASK),
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AtlasPage", () => {
  it("默认加载 demo 任务并显示状态、节点与报告", async () => {
    render(<AtlasPage />);
    expect(await screen.findByText(/爱因斯坦出生于哪一年/)).toBeInTheDocument();
    expect(screen.getByText("executing")).toBeInTheDocument();
    expect(screen.getByText("1879 年")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "任务报告" })).toBeInTheDocument();
    expect(screen.getByText(/状态: executing/)).toBeInTheDocument();
  });

  it("输入会话 ID 后重新拉取对应任务", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    const input = screen.getByLabelText("会话 ID");
    fireEvent.change(input, { target: { value: "session-42" } });
    fireEvent.click(screen.getByRole("button", { name: "加载任务" }));
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    expect(calls.some(([url]) => String(url).includes("/atlas-api/tasks/session-42"))).toBe(true);
  });
});
