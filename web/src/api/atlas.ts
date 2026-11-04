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
  /** 这套会话跑的控制流：legacy_research_v1（固定三段式）或 plan_dag_v1（显式计划） */
  workflow_version: string;
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

export interface AtlasEvidence {
  source?: string;
  chunk_id?: string;
  quote?: string;
  retriever?: string;
  rank?: number;
}

export interface ResearchCallResult {
  session_id: string;
  fact_id: string;
  answer: string;
  evidence: AtlasEvidence[];
  confidence: number;
  stop_reason: string;
  trace_id: string;
  failure_kind: string;
  failure_message: string;
}

export async function runAtlasResearch(
  sessionId: string,
  query: string,
): Promise<ResearchCallResult> {
  const res = await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/research`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      query,
      request_id: `req-${Date.now()}`,
      idempotency_key: `research-${sessionId}-${Date.now()}`,
    }),
  });
  return json<ResearchCallResult>(res);
}

export interface OrchestrateResult {
  session_id: string;
  state: string;
  answer: string;
  route: string;
  draft: string;
  calls: string[];
  fact_ids: string[];
  research_trace_id: string;
  research_error: Record<string, unknown> | null;
}

export async function runAtlasOrchestration(sessionId: string): Promise<OrchestrateResult> {
  const res = await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/orchestrate`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ request_id: `req-${Date.now()}` }),
  });
  return json<OrchestrateResult>(res);
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

// --------------------------------------------------------------------------- //
// 计划（plan_dag_v1）与工具调用：同一页要能看到"这一步做到哪、外部调用有没有结论"
// --------------------------------------------------------------------------- //

export interface AtlasStep {
  step_id: string;
  title: string;
  kind: string;
  status: string;
  ordinal: number;
  tool_name: string;
  attempt_count: number;
  status_reason: string;
  stale_reason: string;
}

export interface AtlasDependency {
  from_step_id: string;
  to_step_id: string;
  kind: string;
  binding: Record<string, string>;
}

export interface AtlasPlan {
  plan_id: string;
  revision: number;
  status: string;
  summary: string;
  strategy: string;
  generated_by: string;
  validator_version: string;
  created_at: string;
  activated_at: string;
  validation: Record<string, unknown>;
}

export interface AtlasPlanDetail {
  session_id: string;
  plan: AtlasPlan;
  steps: AtlasStep[];
  dependencies: AtlasDependency[];
}

export interface AtlasPlanAdvanceResult {
  session_id: string;
  plan_id: string;
  state: string;
  workflow_version: string;
  executed: string[];
  needs_human: string[];
  failed: string[];
  recovered_in_flight: string[];
  stopped_reason: string;
  complete: boolean;
  steps: AtlasStep[];
}

export interface AtlasToolCall {
  tool_call_id: string;
  tool_name: string;
  plan_id: string;
  step_id: string;
  effect_class: string;
  status: string;
  outcome: string;
  idempotency_key: string;
  args_fingerprint: string;
  retry_count: number;
  reconcile_attempts: number;
  compensation_for: string;
  result: Record<string, unknown>;
  error: Record<string, unknown>;
  dispatched_at: string;
  settled_at: string;
}

export async function getAtlasPlans(sessionId: string): Promise<AtlasPlan[]> {
  const body = await json<{ session_id: string; plans: AtlasPlan[] }>(
    await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/plans`),
  );
  return body.plans ?? [];
}

export async function getAtlasPlanDetail(
  sessionId: string,
  planId: string,
): Promise<AtlasPlanDetail> {
  return json<AtlasPlanDetail>(
    await fetch(
      `${BASE}/tasks/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}`,
    ),
  );
}

export async function advanceAtlasPlan(
  sessionId: string,
  planId: string,
): Promise<AtlasPlanAdvanceResult> {
  const res = await fetch(
    `${BASE}/tasks/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}/advance`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ request_id: `req-${Date.now()}`, max_steps: 20 }),
    },
  );
  return json<AtlasPlanAdvanceResult>(res);
}

export async function getAtlasToolCalls(sessionId: string): Promise<AtlasToolCall[]> {
  const body = await json<{ session_id: string; tool_calls: AtlasToolCall[] }>(
    await fetch(`${BASE}/tasks/${encodeURIComponent(sessionId)}/tool-calls`),
  );
  return body.tool_calls ?? [];
}

export async function reconcileAtlasToolCall(
  sessionId: string,
  toolCallId: string,
  outcome: string,
  detail = "工作台人工对账",
): Promise<AtlasToolCall> {
  const res = await fetch(
    `${BASE}/tasks/${encodeURIComponent(sessionId)}/tool-calls/${encodeURIComponent(toolCallId)}/reconcile`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ outcome, detail, request_id: `req-${Date.now()}` }),
    },
  );
  return json<AtlasToolCall>(res);
}

export interface AtlasRecovery {
  session_id: string;
  plan_id: string;
  plan_status: string;
  action: string;          // resume / blocked_on_reconcile / blocked_on_human / nothing_to_do
  blocked: boolean;
  reason: string;
  in_flight_tool_calls: {
    tool_call_id: string; tool_name: string; step_id: string;
    status: string; outcome: string;
  }[];
  pending_steps: string[];
  dispatched_steps: string[];
  stale_steps: string[];
  needs_human_steps: string[];
  failed_steps: string[];
  done_steps: string[];
  executed: string[];
  recovered_in_flight: string[];
  stopped_reason: string;
  complete: boolean;
  state: string;
}

async function _recover(
  sessionId: string,
  planId: string,
  dryRun: boolean,
): Promise<AtlasRecovery> {
  const res = await fetch(
    `${BASE}/tasks/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(planId)}/recover`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun, request_id: `req-${Date.now()}` }),
    },
  );
  return json<AtlasRecovery>(res);
}

/** 只读评估：回答"能不能接着跑、卡在哪"，不动任何状态。 */
export function assessAtlasRecovery(sessionId: string, planId: string): Promise<AtlasRecovery> {
  return _recover(sessionId, planId, true);
}

/** 真恢复：只在没有在途未知调用时才真的续跑。 */
export function recoverAtlasPlan(sessionId: string, planId: string): Promise<AtlasRecovery> {
  return _recover(sessionId, planId, false);
}
