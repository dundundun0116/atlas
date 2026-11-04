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
  workflow_version: "legacy_research_v1",
};

const REPORT = { session_id: "demo", state: "planning", report: "# 任务报告\n状态: planning" };

const PLANS = [
  {
    plan_id: "P1", revision: 1, status: "active", summary: "研究流程",
    strategy: "dag", generated_by: "atlas/plan_dag_v1",
    validator_version: "plan-validator/1", created_at: "", activated_at: "",
    validation: { ok: true, activatable: true, issues: [] },
  },
];

const PLAN_DETAIL = {
  session_id: "demo",
  plan: PLANS[0],
  steps: [
    { step_id: "retrieve", title: "检索证据", kind: "tool", status: "needs_human",
      ordinal: 10, tool_name: "compassrag", attempt_count: 1,
      status_reason: "结果未知，需对账", stale_reason: "" },
    { step_id: "draft", title: "起草答案", kind: "llm", status: "blocked",
      ordinal: 20, tool_name: "", attempt_count: 0, status_reason: "", stale_reason: "" },
  ],
  dependencies: [
    { from_step_id: "retrieve", to_step_id: "draft", kind: "data",
      binding: { output_name: "refs", input_name: "evidence" } },
  ],
  revisions: [],
};

const RECOVERY = {
  session_id: "demo", plan_id: "P1", plan_status: "active",
  action: "resume", blocked: false,
  reason: "可以接着跑：只补未提交的那一步与未完成的 Step",
  in_flight_tool_calls: [], pending_steps: ["draft", "verify"],
  dispatched_steps: [], stale_steps: [], needs_human_steps: [],
  failed_steps: [], done_steps: ["retrieve"],
  executed: [], recovered_in_flight: [], stopped_reason: "", complete: false,
  state: "planning",
};

const RECOVERY_BLOCKED = {
  ...RECOVERY,
  action: "blocked_on_reconcile",
  blocked: true,
  reason: "1 个工具调用结果未知，必须先对账（重复发可能造成重复副作用）",
  in_flight_tool_calls: [
    { tool_call_id: "tc-1", tool_name: "compassrag", step_id: "retrieve",
      status: "indeterminate", outcome: "unknowable" },
  ],
};

const TOOL_CALLS = [
  {
    tool_call_id: "tc-1", tool_name: "compassrag", plan_id: "P1", step_id: "retrieve",
    effect_class: "read_only", status: "indeterminate", outcome: "unknowable",
    idempotency_key: "compassrag:demo/retrieve:abcdef1234567890",
    args_fingerprint: "abc", retry_count: 0, reconcile_attempts: 1,
    compensation_for: "", result: {}, error: {},
    dispatched_at: "", settled_at: "",
  },
];

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
    if (String(url).includes("/recover")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (body.dry_run) return respond(RECOVERY);
      return respond({ ...RECOVERY, executed: ["draft", "verify"], complete: true });
    }
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
    if (String(url).includes("/tool-calls/")) {
      return respond({ ...TOOL_CALLS[0], status: "reconciled", outcome: "applied" });
    }
    if (String(url).includes("/tool-calls")) return respond({ session_id: "demo", tool_calls: TOOL_CALLS });
    if (String(url).includes("/plans/P1")) return respond(PLAN_DETAIL);
    if (String(url).includes("/plans")) return respond({ session_id: "demo", plans: PLANS });
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

  it("显示计划与每个 Step 的状态", async () => {
    render(<AtlasPage />);
    expect(await screen.findByRole("heading", { name: "计划与步骤" })).toBeInTheDocument();
    expect(screen.getByText("检索证据")).toBeInTheDocument();
    expect(screen.getAllByText(/需人工/).length).toBeGreaterThan(0);
    expect(screen.getByText(/refs→evidence/)).toBeInTheDocument();
  });

  it("结果未知的工具调用给出对账按钮而不是重发", async () => {
    render(<AtlasPage />);
    expect(await screen.findByRole("heading", { name: "工具调用与对账" })).toBeInTheDocument();
    expect(screen.getByText(/先对账，不要重发/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认已生效" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重发/ })).toBeNull();
  });

  it("点对账按钮提交结论并刷新", async () => {
    render(<AtlasPage />);
    await screen.findByRole("heading", { name: "工具调用与对账" });
    fireEvent.click(screen.getByRole("button", { name: "确认未生效" }));
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        ([url, init]) =>
          init?.method === "POST" && String(url).includes("/tool-calls/tc-1/reconcile"),
      );
      expect(posts.length).toBe(1);
      const body = JSON.parse(String((posts[0][1] as RequestInit).body));
      expect(body.outcome).toBe("not_applied");
    });
  });

  it("推进计划按钮调用 advance 并刷新", async () => {
    render(<AtlasPage />);
    await screen.findByRole("heading", { name: "计划与步骤" });
    fireEvent.click(screen.getByRole("button", { name: /推进计划/ }));
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        ([url, init]) =>
          init?.method === "POST" && String(url).includes("/plans/P1/advance"),
      );
      expect(posts.length).toBe(1);
    });
  });

  it("显示恢复评估：能接着跑时给恢复按钮", async () => {
    render(<AtlasPage />);
    expect(await screen.findByRole("heading", { name: "恢复" })).toBeInTheDocument();
    expect(screen.getByText(/可以接着跑/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "恢复并继续" })).toBeEnabled();
  });

  it("有在途未知调用时恢复按钮被禁用并说明原因", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/recover")) {
        const body = JSON.parse(String(init?.body ?? "{}"));
        return respond(body.dry_run ? RECOVERY_BLOCKED : RECOVERY_BLOCKED);
      }
      if (String(url).includes("/tool-calls")) {
        return respond({ session_id: "demo", tool_calls: TOOL_CALLS });
      }
      if (String(url).includes("/plans/P1")) return respond(PLAN_DETAIL);
      if (String(url).includes("/plans")) return respond({ session_id: "demo", plans: PLANS });
      return String(url).includes("/report") ? respond(REPORT) : respond(TASK);
    });
    render(<AtlasPage />);
    await screen.findByRole("heading", { name: "恢复" });
    expect(screen.getByText(/先在上面的/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "当前不能自动恢复" })).toBeDisabled();
  });

  it("点恢复按钮调用 recover 并刷新", async () => {
    render(<AtlasPage />);
    await screen.findByRole("heading", { name: "恢复" });
    fireEvent.click(screen.getByRole("button", { name: "恢复并继续" }));
    await waitFor(() => {
      const posts = fetchMock.mock.calls.filter(
        ([url, init]) =>
          init?.method === "POST" && String(url).includes("/plans/P1/recover"),
      );
      const real = posts.filter(([, init]) => {
        const body = JSON.parse(String((init as RequestInit).body));
        return body.dry_run === false;
      });
      expect(real.length).toBe(1);
    });
  });

  it("创建任务后自动加载该会话", async () => {
    render(<AtlasPage />);
    await screen.findByText(/爱因斯坦出生于哪一年/);
    fireEvent.change(screen.getByLabelText("会话 ID"), { target: { value: "new-session" } });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));
    await waitFor(() => {
      const creates = fetchMock.mock.calls.filter(
        ([url, init]) =>
          init?.method === "POST" && String(url).endsWith("/atlas-api/tasks"),
      );
      expect(creates.length).toBe(1);
    });
  });
});

describe("重规划记录面板", () => {
  it("有重规划时显示触发、失效与保持有效的步骤", async () => {
    const base = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/plans/P1")
          && !String(url).includes("/recover")
          && !String(url).includes("/advance")
          && init?.method !== "POST") {
        return respond({
          ...PLAN_DETAIL,
          revisions: [{
            revision_id: "r1", number: 1, trigger: "environment_changed",
            changed_steps: ["write"], affected_steps: ["confirm"],
            stale_steps: [], reused_steps: ["other"],
            changes: [{ target: "order-1", kind: "created", before: "", after: "abc" }],
            reason: "order-1 已经存在，不能重复创建", created_at: "",
          }],
        });
      }
      return (base as any)(url, init);
    });

    render(<AtlasPage />);
    expect(await screen.findByRole("heading", { name: "重规划记录" })).toBeInTheDocument();
    expect(screen.getByText("environment_changed")).toBeInTheDocument();
    expect(screen.getByText("order-1 已经存在，不能重复创建")).toBeInTheDocument();
    expect(screen.getByText("other")).toBeInTheDocument();
  });

  it("没有重规划时明确说明", async () => {
    render(<AtlasPage />);
    expect(await screen.findByRole("heading", { name: "重规划记录" })).toBeInTheDocument();
    expect(screen.getByText(/还没有发生局部重规划/)).toBeInTheDocument();
  });
});
