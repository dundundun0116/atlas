import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AtlasPage from "./AtlasPage";

const TASK = {
  session_id: "demo",
  goal: "回答：爱因斯坦出生于哪一年？",
  state: "planning",
  failure_reason: "",
  allowed_triggers: ["plan_ready", "pause"],
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

const REPORT = { session_id: "demo", state: "planning", report: "# 任务报告\n状态: planning" };

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
      if (String(url).includes("/orchestrate")) {
        return respond({
          session_id: "demo", state: "completed", answer: "编排答案",
          route: "先检索", draft: "草稿", calls: ["planner", "worker", "auditor"],
          fact_ids: ["F1"], research_trace_id: "compass-trace-1", research_error: null,
        });
      }
      if (String(url).includes("/research")) {
        return respond({
          session_id: "demo", fact_id: "F2", answer: "1879 年出生（API）",
          evidence: [{ source: "Albert Einstein", chunk_id: "c2", quote: "出生于乌尔姆。",
                       retriever: "kg", rank: 2 }],
          confidence: 0.75, stop_reason: "answered", trace_id: "compass-trace-1",
          failure_kind: "", failure_message: "",
        });
      }
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
    expect(screen.getByText("planning")).toBeInTheDocument();
    expect(screen.getByText("1879 年")).toBeInTheDocument();
    expect(screen.getByText(/bm25/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "任务报告" })).toBeInTheDocument();
    expect(screen.getByText(/状态: planning/)).toBeInTheDocument();
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

  it("点击推进按钮调用 advance 并刷新", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.click(screen.getByRole("button", { name: "规划完成" }));
    await waitFor(() => {
      const advances = fetchMock.mock.calls.filter(
        ([url, init]) => init?.method === "POST" && String(url).includes("/advance"),
      );
      expect(advances.length).toBe(1);
      expect(String(advances[0][0])).toContain("/atlas-api/tasks/demo/advance");
      const body = JSON.parse(String((advances[0][1] as RequestInit).body));
      expect(body.trigger).toBe("plan_ready");
    });
  });

  it("点击运行完整编排显示答案与调用链", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.click(screen.getByRole("button", { name: /运行完整编排/ }));
    expect(await screen.findByText("编排答案")).toBeInTheDocument();
    expect(screen.getByText(/planner → worker → auditor/)).toBeInTheDocument();
    const calls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(calls.some((url) => url.includes("/atlas-api/tasks/demo/orchestrate"))).toBe(true);
  });

  it("点击发起检索显示答案/证据并刷新任务", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.click(screen.getByRole("button", { name: "发起检索" }));
    expect(await screen.findByText("1879 年出生（API）")).toBeInTheDocument();
    expect(screen.getByText(/compass-trace-1/)).toBeInTheDocument();
    const calls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(calls.some((url) => url.includes("/atlas-api/tasks/demo/research"))).toBe(true);
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
