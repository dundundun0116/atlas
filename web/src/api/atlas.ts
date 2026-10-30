/**
 * Atlas 任务 API 客户端（统一工作台页用）。
 *
 * 与 `client.ts` 的分工：这里只读 Atlas 的任务状态/报告，走 Vite 的 `/atlas-api` 代理
 * → `scripts/serve_atlas_api.py`（默认 127.0.0.1:8001）。不做任务写入，
 * 创建/推进任务仍走 Atlas API 或 CLI。
 */

const BASE = "/atlas-api";

export interface AtlasNode {
  id: string;
  type: string;
  status: string;
  content: string;
  actor: string;
  source: string;
  confidence: number;
  valid_until: string;
  verified_at: string;
  reason: string;
  evidence: string;
  meta: Record<string, unknown>;
}

export interface AtlasTask {
  session_id: string;
  goal: string;
  state: string;
  failure_reason: string;
  allowed_triggers: string[];
  nodes: AtlasNode[];
  edges: { src: string; dst: string; type: string }[];
  checkpoint: Record<string, unknown> | null;
  provenance: Record<string, unknown>;
}

export interface AtlasReport {
  session_id: string;
  state: string;
  report: string;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export async function getAtlasTask(sessionId: string): Promise<AtlasTask> {
  return json<AtlasTask>(await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}`));
}

export interface CreateTaskPayload {
  session_id: string;
  goal: string;
  request_id?: string;
  idempotency_key?: string;
}

export async function createAtlasTask(payload: CreateTaskPayload): Promise<{ session_id: string; state: string }> {
  const res = await fetch(`${BASE}/tasks`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return json<{ session_id: string; state: string }>(res);
}

export async function advanceAtlasTask(
  sessionId: string,
  trigger: string,
  reason = "工作台推进",
): Promise<{ session_id: string; state: string }> {
  const res = await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/advance`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      trigger,
      reason,
      request_id: `req-${Date.now()}`,
      idempotency_key: `advance-${sessionId}-${trigger}-${Date.now()}`,
    }),
  });
  return json<{ session_id: string; state: string }>(res);
}

export async function pauseAtlasTask(
  sessionId: string,
  reason = "工作台暂停",
): Promise<{ session_id: string; state: string }> {
  const res = await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/pause`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason, request_id: `req-${Date.now()}` }),
  });
  return json<{ session_id: string; state: string }>(res);
}

export async function resumeAtlasTask(
  sessionId: string,
): Promise<{ session_id: string; state: string }> {
  const res = await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/resume`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ request_id: `req-${Date.now()}` }),
  });
  return json<{ session_id: string; state: string }>(res);
}

export async function getAtlasReport(sessionId: string): Promise<AtlasReport> {
  return json<AtlasReport>(
    await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/report`),
  );
}
