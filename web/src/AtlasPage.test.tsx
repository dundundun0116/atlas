import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AtlasPage from "./AtlasPage";

const TASK = {
  session_id: "demo",
  goal: "回答：爱因斯坦出生于哪一年？",
  state: "executing",
  failure_reason: "",
  nodes: [
    {
      id: "D1", type: "decision", status: "active", content: "先检索再作答",
      actor: "planner", source: "", confidence: 0, valid_until: "",
      verified_at: "", reason: "事实型问题", evidence: "", meta: {},
    },
    {
      id: "F1", type: "fact", status: "active", content: "1879 年",
      actor: "research", source: "Albert Einstein", confidence: 0.8,
      valid_until: "", verified_at: "",
      reason: "检索 1 轮",
      evidence: JSON.stringify([
        { source: "Albert Einstein", chunk_id: "c1", quote: "1879 年出生于乌尔姆。",
          retriever: "bm25", rank: 1 },
      ]),
      meta: { research_trace_id: "trace-abc" },
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

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      return respond({ session_id: "session-42", state: "planning", request_id: "r", replayed: false });
    }
    return String(url).includes("/report") ? respond(REPORT) : respond(TASK);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AtlasPage", () => {
  it("默认加载 demo 任务并显示状态、证据与报告", async () => {
    render(<AtlasPage />);
    expect(await screen.findByText(/爱因斯坦出生于哪一年/)).toBeInTheDocument();
    expect(screen.getByText("executing")).toBeInTheDocument();
    expect(screen.getByText("1879 年")).toBeInTheDocument();
    expect(screen.getByText(/bm25/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "任务报告" })).toBeInTheDocument();
    expect(screen.getByText(/状态: executing/)).toBeInTheDocument();
  });

  it("输入会话 ID 后重新拉取对应任务", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.change(screen.getByLabelText("会话 ID"), { target: { value: "session-42" } });
    fireEvent.click(screen.getByRole("button", { name: "加载任务" }));
    await waitFor(() => {
      const calls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(calls.some((url) => url.includes("/atlas-api/tasks/session-42"))).toBe(true);
    });
  });

  it("创建任务后自动加载该会话", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.change(screen.getByLabelText("会话 ID"), { target: { value: "new-session" } });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
      expect(posts.length).toBe(1);
      expect(String(posts[0][0])).toContain("/atlas-api/tasks");
    });
  });
});
