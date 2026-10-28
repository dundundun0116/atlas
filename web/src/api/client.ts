/**
 * API 客户端。类型全部来自 `./types`（由 `scripts/export_openapi.py` +
 * `openapi-typescript` 从后端 OpenAPI 生成），组件里不复制后端 schema。
 */
import type { components } from "./types";

export type ResearchRequest = components["schemas"]["ResearchRequest"];
export type ResearchResult = components["schemas"]["ResearchResult"];
export type ResearchEvidence = components["schemas"]["ResearchEvidence"];
export type RunSummary = components["schemas"]["RunSummary"];
export type TraceEvent = components["schemas"]["TraceEvent"];
export type RunEnvelope = components["schemas"]["RunEnvelope"];
export type HealthResponse = components["schemas"]["HealthResponse"];

const BASE = "";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export async function getHealth(): Promise<HealthResponse> {
  return json<HealthResponse>(await fetch(`${BASE}/health`));
}

/** 同步：等答案返回结构化结果（不用于长任务，前端默认走异步 + SSE）。 */
export async function submitSync(payload: ResearchRequest): Promise<ResearchResult> {
  const res = await fetch(`${BASE}/research/sync`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<ResearchResult>(res);
}

/** 异步：立刻拿到 run_id，随后用 SSE 订阅进度。 */
export async function submitAsync(payload: ResearchRequest): Promise<RunSummary> {
  const res = await fetch(`${BASE}/research/async`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<RunSummary>(res);
}

export async function getRun(runId: string): Promise<RunEnvelope> {
  return json<RunEnvelope>(await fetch(`${BASE}/runs/${encodeURIComponent(runId)}`));
}

export async function listRuns(limit = 30): Promise<RunSummary[]> {
  return json<RunSummary[]>(await fetch(`${BASE}/runs?limit=${limit}`));
}

export async function cancelRun(runId: string): Promise<{ cancelled: boolean; status: string }> {
  const res = await fetch(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  return json(res);
}

export const EVENT_KINDS = [
  "status",
  "plan",
  "retrieve",
  "verify",
  "repair",
  "answer",
  "finish",
  "error",
  "end",
] as const;

/**
 * 订阅运行事件（SSE）。返回一个关闭函数；组件卸载时务必调用，
 * 后端不会因为订阅断开而中断运行。
 */
export function subscribeEvents(
  runId: string,
  onEvent: (event: TraceEvent) => void,
  onDone?: () => void,
): () => void {
  const source = new EventSource(`${BASE}/runs/${encodeURIComponent(runId)}/events`);
  const handlers: Array<[string, (ev: MessageEvent) => void]> = [];

  for (const kind of EVENT_KINDS) {
    const handler = (ev: MessageEvent) => {
      let parsed: TraceEvent;
      try {
        parsed = JSON.parse(ev.data) as TraceEvent;
      } catch {
        return;
      }
      onEvent(parsed);
      if (kind === "end") {
        source.close();
        onDone?.();
      }
    };
    handlers.push([kind, handler]);
    source.addEventListener(kind, handler as EventListener);
  }

  const close = () => {
    for (const [kind, handler] of handlers) {
      source.removeEventListener(kind, handler as EventListener);
    }
    source.close();
  };
  return close;
}

/** 事件 → 一句话摘要（时间线渲染用；纯函数便于测试）。 */
export function describeEvent(event: TraceEvent): string {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  switch (event.kind) {
    case "plan": {
      const route = String(payload["detail"] && (payload["detail"] as any).route || "");
      return `规划：${route || "single"}`;
    }
    case "retrieve": {
      const detail = (payload["detail"] ?? {}) as Record<string, unknown>;
      const query = String(detail["query"] ?? "");
      const tools = Array.isArray(detail["tools"]) ? (detail["tools"] as any[]) : [];
      const names = tools.map((t) => String(t?.tool ?? "")).filter(Boolean).join("+");
      return `检索（${names || "?"}）：${query}`;
    }
    case "verify":
      return "证据判断";
    case "repair":
      return "修复";
    case "answer":
      return "生成答案";
    case "finish":
      return "结束";
    case "error":
      return "失败";
    default:
      return event.kind;
  }
}
