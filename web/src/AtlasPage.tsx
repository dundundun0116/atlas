import { useCallback, useEffect, useState } from "react";
import {
  advanceAtlasPlan,
  advanceAtlasTask,
  assessAtlasRecovery,
  createAtlasTask,
  getAtlasPlanDetail,
  getAtlasPlans,
  getAtlasReport,
  getAtlasTask,
  getAtlasToolCalls,
  pauseAtlasTask,
  reconcileAtlasToolCall,
  resumeAtlasTask,
  runAtlasOrchestration,
  recoverAtlasPlan,
  runAtlasResearch,
  type AtlasEvidence,
  type AtlasNode,
  type AtlasPlan,
  type AtlasPlanDetail,
  type AtlasRecovery,
  type AtlasReport,
  type AtlasTask,
  type AtlasToolCall,
  type OrchestrateResult,
  type ResearchCallResult,
} from "./api/atlas";

const DEFAULT_SESSION =
  new URLSearchParams(window.location.search).get("session") ?? "demo";

function parseEvidence(raw: string): AtlasEvidence[] {
  try {
    const items = JSON.parse(raw || "[]");
    return Array.isArray(items) ? items : [];
  } catch {
    return [];
  }
}

const STEP_LABELS: Record<string, string> = {
  done: "✅ 完成",
  failed: "❌ 失败",
  needs_human: "🙋 需人工",
  stale: "♻️ 已失效",
  running: "▶️ 执行中",
  dispatched: "📤 已派发",
  verifying: "🔍 核验中",
  blocked: "⛔ 依赖未满足",
  pending: "⏳ 待调度",
  ready: "🟢 可调度",
  skipped: "⏭️ 跳过",
};

const TOOL_STATUS_LABELS: Record<string, string> = {
  settled: "已结算",
  failed_determinate: "确定失败",
  failed_partial: "部分成功",
  indeterminate: "结果未知",
  reconciling: "对账中",
  reconciled: "已对账",
  compensating: "补偿中",
  dispatched: "已派发",
  proposed: "已登记意图",
};

const TRIGGER_LABELS: Record<string, string> = {
  plan_ready: "规划完成",
  work_done: "执行完成",
  audit_pass: "审核通过",
  audit_repair: "打回修订",
  repair_done: "修订完成",
  pause: "暂停",
};

function evidenceLabel(item: AtlasEvidence): string {
  const source = item.source || item.chunk_id || "未知来源";
  const retriever = item.retriever ? ` · ${item.retriever}` : "";
  const rank = item.rank ? ` #${item.rank}` : "";
  return `${source}${retriever}${rank}`;
}

/**
 * 统一工作台（只读）：同一页看 Atlas 任务的状态、角色步骤、图节点与任务报告。
 * 研究侧（问题路由 / 检索轨迹）仍在 `/` 的 CompassRAG 工作台；两页通过代理分开。
 */
function EvidenceList({ node }: { node: AtlasNode }) {
  const items = parseEvidence(node.evidence);
  if (items.length === 0) return null;
  return (
    <ul className="atlas-evidence">
      {items.map((item, index) => (
        <li key={index}>
          <strong>{evidenceLabel(item)}</strong>
          {item.quote ? ` — ${item.quote}` : ""}
        </li>
      ))}
    </ul>
  );
}

export default function AtlasPage() {
  const [sessionId, setSessionId] = useState(DEFAULT_SESSION);
  const [goal, setGoal] = useState("回答：爱因斯坦出生于哪一年？");
  const [researchQuery, setResearchQuery] = useState("爱因斯坦出生于哪一年？");
  const [research, setResearch] = useState<ResearchCallResult | null>(null);
  const [researching, setResearching] = useState(false);
  const [orchestration, setOrchestration] = useState<OrchestrateResult | null>(null);
  const [orchestrating, setOrchestrating] = useState(false);
  const [task, setTask] = useState<AtlasTask | null>(null);
  const [report, setReport] = useState<AtlasReport | null>(null);
  const [plans, setPlans] = useState<AtlasPlan[]>([]);
  const [planDetail, setPlanDetail] = useState<AtlasPlanDetail | null>(null);
  const [toolCalls, setToolCalls] = useState<AtlasToolCall[]>([]);
  const [recovery, setRecovery] = useState<AtlasRecovery | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (id: string) => {
    const target = id.trim();
    if (!target) return;
    setLoading(true);
    setError(null);
    try {
      const [state, summary] = await Promise.all([
        getAtlasTask(target),
        getAtlasReport(target),
      ]);
      setTask(state);
      setReport(summary);
      // 计划与工具调用是"可选的"：老会话没有计划层，取不到不该让整页报错
      const [planList, calls] = await Promise.all([
        getAtlasPlans(target).catch(() => [] as AtlasPlan[]),
        getAtlasToolCalls(target).catch(() => [] as AtlasToolCall[]),
      ]);
      setPlans(planList);
      setToolCalls(calls);
      const active = planList.find((item) => item.status === "active") ?? planList.at(-1);
      const detail = active
        ? await getAtlasPlanDetail(target, active.plan_id).catch(() => null)
        : null;
      setPlanDetail(detail);
      // 只读评估：能不能接着跑、卡在哪（不动任何状态）
      setRecovery(
        detail && detail.plan.status === "active"
          ? await assessAtlasRecovery(target, detail.plan.plan_id).catch(() => null)
          : null,
      );
    } catch (e) {
      setTask(null);
      setReport(null);
      setPlans([]);
      setPlanDetail(null);
      setToolCalls([]);
      setRecovery(null);
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const act = useCallback(
    async (operation: () => Promise<unknown>) => {
      setLoading(true);
      setError(null);
      try {
        await operation();
        await load(sessionId);
      } catch (e) {
        setError(String(e));
        setLoading(false);
      }
    },
    [load, sessionId],
  );

  const doResearch = useCallback(async () => {
    const query = researchQuery.trim();
    if (!query) return;
    setResearching(true);
    setError(null);
    try {
      const result = await runAtlasResearch(sessionId, query);
      setResearch(result);
      if (result.fact_id) await load(sessionId);   // 成功写 Fact：刷新节点与证据
    } catch (e) {
      setError(String(e));
    } finally {
      setResearching(false);
    }
  }, [load, researchQuery, sessionId]);

  const doOrchestrate = useCallback(async () => {
    setOrchestrating(true);
    setError(null);
    try {
      const result = await runAtlasOrchestration(sessionId);
      setOrchestration(result);
      await load(sessionId);
    } catch (e) {
      setError(String(e));
    } finally {
      setOrchestrating(false);
    }
  }, [load, sessionId]);

  const doAdvancePlan = useCallback(async () => {
    if (!planDetail) return;
    setLoading(true);
    setError(null);
    try {
      await advanceAtlasPlan(sessionId, planDetail.plan.plan_id);
      await load(sessionId);
    } catch (e) {
      setError(String(e));
      setLoading(false);
    }
  }, [load, planDetail, sessionId]);

  const doRecover = useCallback(async () => {
    if (!planDetail) return;
    setLoading(true);
    setError(null);
    try {
      await recoverAtlasPlan(sessionId, planDetail.plan.plan_id);
      await load(sessionId);
    } catch (e) {
      setError(String(e));
      setLoading(false);
    }
  }, [load, planDetail, sessionId]);

  const doReconcile = useCallback(
    async (toolCallId: string, outcome: string) => {
      setLoading(true);
      setError(null);
      try {
        await reconcileAtlasToolCall(sessionId, toolCallId, outcome);
        await load(sessionId);
      } catch (e) {
        setError(String(e));
        setLoading(false);
      }
    },
    [load, sessionId],
  );

  const create = useCallback(async () => {
    const target = sessionId.trim();
    if (!target || !goal.trim()) return;
    setLoading(true);
    setError(null);
    try {
      await createAtlasTask({
        session_id: target,
        goal: goal.trim(),
        idempotency_key: `create-${target}`,
        request_id: `req-${Date.now()}`,
      });
      await load(target);
    } catch (e) {
      setError(String(e));
      setLoading(false);
    }
  }, [goal, load, sessionId]);

  useEffect(() => {
    document.title = "Atlas · 任务工作台";
    load(DEFAULT_SESSION);
  }, [load]);

  return (
    <div className="app">
      <header>
        <h1>Atlas · 任务工作台</h1>
        <p className="muted">
          任务控制台：创建 / 推进 / 暂停 / 恢复 / 发起检索 / 运行完整编排，状态、
          角色步骤、图节点与证据来自 Atlas API（<code>scripts/serve_atlas_api.py</code>）。
          单题研究页仍在 <a href="/">CompassRAG 工作台</a>。
        </p>
      </header>

      <section className="panel">
        <div className="controls">
          <label htmlFor="session">会话 ID</label>
          <input
            id="session"
            value={sessionId}
            onChange={(event) => setSessionId(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") load(sessionId);
            }}
            placeholder="demo"
          />
          <button onClick={() => load(sessionId)} disabled={loading}>
            {loading ? "加载中…" : "加载任务"}
          </button>
        </div>
        <div className="controls atlas-create">
          <label htmlFor="goal">新任务目标</label>
          <input
            id="goal"
            value={goal}
            onChange={(event) => setGoal(event.target.value)}
            placeholder="例如：回答：爱因斯坦出生于哪一年？"
          />
          <button onClick={create} disabled={loading}>
            创建任务
          </button>
        </div>
        {error && <p className="error">请求失败：{error}</p>}
      </section>

      {task && (
        <>
          <section className="panel">
            <div className="meta">
              <span>
                状态 <span className="badge">{task.state}</span>
              </span>
              <span className="small">节点 {task.nodes.length}</span>
              <span className="small">边 {task.edges.length}</span>
            </div>
            <p className="question">{task.goal || "（未登记目标）"}</p>
            {task.failure_reason && (
              <p className="error">失败原因：{task.failure_reason}</p>
            )}
            <div className="atlas-actions">
              {task.allowed_triggers.map((trigger) => (
                <button
                  key={trigger}
                  disabled={loading}
                  onClick={() => act(() => advanceAtlasTask(sessionId, trigger))}
                >
                  {TRIGGER_LABELS[trigger] ?? trigger}
                </button>
              ))}
              {task.state === "paused" && (
                <button disabled={loading} onClick={() => act(() => resumeAtlasTask(sessionId))}>
                  恢复
                </button>
              )}
            </div>
          </section>

          {planDetail && (
            <section className="panel">
              <h2>计划与步骤</h2>
              <p className="small muted">
                计划 <code>{planDetail.plan.plan_id}</code> rev{planDetail.plan.revision}
                {" · "}
                {planDetail.plan.status}
                {" · "}
                {planDetail.plan.validator_version}
                {" · "}
                工作流 <code>{task.workflow_version || planDetail.plan.strategy}</code>
              </p>
              <table className="atlas-nodes">
                <thead>
                  <tr>
                    <th>Step</th>
                    <th>标题</th>
                    <th>类型</th>
                    <th>状态</th>
                    <th>尝试</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {planDetail.steps.map((step) => (
                    <tr key={step.step_id}>
                      <td>
                        <code>{step.step_id}</code>
                      </td>
                      <td>{step.title}</td>
                      <td>{step.kind}</td>
                      <td>{STEP_LABELS[step.status] ?? step.status}</td>
                      <td>{step.attempt_count}</td>
                      <td className="small muted">
                        {step.status_reason || step.stale_reason || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="small muted">
                依赖：
                {planDetail.dependencies.length === 0
                  ? "无"
                  : planDetail.dependencies
                      .map(
                        (dep) =>
                          `${dep.from_step_id}→${dep.to_step_id}` +
                          (dep.binding?.output_name
                            ? `(${dep.binding.output_name}→${dep.binding.input_name})`
                            : ""),
                      )
                      .join("、")}
              </p>
              {planDetail.plan.status === "active" && (
                <button onClick={doAdvancePlan} disabled={loading}>
                  {loading ? "推进中…" : "推进计划（跑可执行的 Step）"}
                </button>
              )}
            </section>
          )}

          {recovery && (
            <section className="panel">
              <h2>恢复</h2>
              <p className="small muted">
                动作 <code>{recovery.action}</code> · {recovery.reason}
              </p>
              <p className="small">
                已完成 {recovery.done_steps.length} · 待推进{" "}
                {recovery.pending_steps.length + recovery.dispatched_steps.length} · 失效{" "}
                {recovery.stale_steps.length} · 需人工 {recovery.needs_human_steps.length} · 在途调用{" "}
                {recovery.in_flight_tool_calls.length}
              </p>
              {recovery.in_flight_tool_calls.length > 0 && (
                <p className="error">
                  有 {recovery.in_flight_tool_calls.length} 个工具调用结果未知——先在上面的
                  「工具调用与对账」里定论，才能恢复（重复发可能造成重复副作用）。
                </p>
              )}
              {recovery.needs_human_steps.length > 0 && (
                <p className="small muted">
                  需要人工确认：{recovery.needs_human_steps.join("、")}
                </p>
              )}
              {recovery.stale_steps.length > 0 && (
                <p className="small muted">
                  已失效、需要重新绑定输入：{recovery.stale_steps.join("、")}
                </p>
              )}
              <button
                onClick={doRecover}
                disabled={loading || recovery.blocked || recovery.action === "nothing_to_do"}
              >
                {recovery.blocked
                  ? "当前不能自动恢复"
                  : recovery.action === "nothing_to_do"
                    ? "没有可恢复的工作"
                    : "恢复并继续"}
              </button>
            </section>
          )}

          {toolCalls.length > 0 && (
            <section className="panel">
              <h2>工具调用与对账</h2>
              <table className="atlas-nodes">
                <thead>
                  <tr>
                    <th>工具</th>
                    <th>Step</th>
                    <th>状态</th>
                    <th>结论</th>
                    <th>重试</th>
                    <th>对账</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {toolCalls.map((call) => (
                    <tr key={call.tool_call_id}>
                      <td>
                        <code>{call.tool_name}</code>
                      </td>
                      <td>{call.step_id || "—"}</td>
                      <td>
                        {TOOL_STATUS_LABELS[call.status] ?? call.status}
                        {call.status === "indeterminate" ? "（先对账，不要重发）" : ""}
                      </td>
                      <td>{call.outcome || "—"}</td>
                      <td>{call.retry_count}</td>
                      <td>{call.reconcile_attempts}</td>
                      <td>
                        {call.status === "indeterminate" ? (
                          <span className="atlas-actions">
                            <button onClick={() => doReconcile(call.tool_call_id, "applied")}>
                              确认已生效
                            </button>
                            <button onClick={() => doReconcile(call.tool_call_id, "not_applied")}>
                              确认未生效
                            </button>
                            <button onClick={() => doReconcile(call.tool_call_id, "unknowable")}>
                              仍无法判断
                            </button>
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          <section className="panel">
            <h2>发起检索（Atlas → ResearchTool）</h2>
            <div className="controls">
              <label htmlFor="research-query">研究问题</label>
              <input
                id="research-query"
                value={researchQuery}
                onChange={(event) => setResearchQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") doResearch();
                }}
              />
              <button onClick={doResearch} disabled={researching || loading}>
                {researching ? "检索中…" : "发起检索"}
              </button>
            </div>
            {task.state === "planning" && (
              <div className="controls atlas-orchestrate">
                <button onClick={doOrchestrate} disabled={orchestrating || loading}>
                  {orchestrating ? "编排中…" : "运行完整编排（Planner → Worker → Auditor）"}
                </button>
                <span className="small muted">需要服务端配置 LLM_API_KEY；会推进任务到终态</span>
              </div>
            )}
            {orchestration && (
              <div className="atlas-research-result">
                <p className="question">{orchestration.answer}</p>
                <p className="small">
                  route {orchestration.route} · 调用{" "}
                  {orchestration.calls.join(" → ") || "—"} · 状态 {orchestration.state}
                  {orchestration.research_trace_id
                    ? ` · trace ${orchestration.research_trace_id}`
                    : ""}
                </p>
              </div>
            )}
            {research && (
              <div className="atlas-research-result">
                {research.failure_kind ? (
                  <p className="error">
                    检索失败：{research.failure_kind} {research.failure_message}
                    {research.trace_id ? `（trace ${research.trace_id}）` : ""}
                  </p>
                ) : (
                  <>
                    <p className="question">{research.answer}</p>
                    <p className="small">
                      confidence {research.confidence.toFixed(2)} ·{" "}
                      {research.stop_reason || "unknown"} · trace{" "}
                      <code>{research.trace_id}</code>
                      {research.fact_id ? ` · Fact ${research.fact_id}` : ""}
                    </p>
                    <ul className="atlas-evidence">
                      {research.evidence.map((item, index) => (
                        <li key={index}>
                          <strong>{evidenceLabel(item)}</strong>
                          {item.quote ? ` — ${item.quote}` : ""}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
          </section>

          <section className="panel">
            <h2>角色步骤与图节点</h2>
            <table className="atlas-nodes">
              <thead>
                <tr>
                  <th>id</th>
                  <th>类型</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>内容</th>
                </tr>
              </thead>
              <tbody>
                {task.nodes.map((node) => (
                  <tr key={node.id}>
                    <td>
                      <code>{node.id}</code>
                    </td>
                    <td>{node.type}</td>
                    <td>{node.actor}</td>
                    <td>{node.status}</td>
                    <td>
                      {node.content}
                      <EvidenceList node={node} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <h2>任务报告</h2>
            <pre className="atlas-report">{report?.report ?? "（报告不可用）"}</pre>
          </section>
        </>
      )}

      {!task && !error && !loading && (
        <p className="muted">
          没有可显示的任务。先运行{" "}
          <code>python scripts/serve_atlas_api.py --demo</code>，或用 Atlas API 创建任务。
        </p>
      )}
    </div>
  );
}
