import { describe, expect, it } from "vitest";
import { describeEvent, type TraceEvent } from "../client";

const ev = (kind: string, payload: Record<string, unknown> = {}): TraceEvent =>
  ({ run_id: "r", seq: 0, kind, ts: 0, payload }) as TraceEvent;

describe("describeEvent", () => {
  it("把事件翻译成时间线文案", () => {
    expect(describeEvent(ev("plan", { detail: { route: "multi" } }))).toBe("规划：multi");
    expect(describeEvent(ev("retrieve", { detail: { query: "q?", tools: [{ tool: "kg" }, { tool: "bm25" }] } })))
      .toBe("检索（kg+bm25）：q?");
    expect(describeEvent(ev("verify"))).toBe("证据判断");
    expect(describeEvent(ev("finish"))).toBe("结束");
    expect(describeEvent(ev("error"))).toBe("失败");
    expect(describeEvent(ev("weird"))).toBe("weird");
  });
});
