import { useCallback, useEffect, useState } from "react";
import {
  getAtlasReport,
  getAtlasTask,
  type AtlasReport,
  type AtlasTask,
} from "./api/atlas";

const DEFAULT_SESSION =
  new URLSearchParams(window.location.search).get("session") ?? "demo";

/**
 * 统一工作台（只读）：同一页看 Atlas 任务的状态、角色步骤、图节点与任务报告。
 * 研究侧（问题路由 / 检索轨迹）仍在 `/` 的 CompassRAG 工作台；两页通过代理分开。
 */
export default function AtlasPage() {
  const [sessionId, setSessionId] = useState(DEFAULT_SESSION);
  const [task, setTask] = useState<AtlasTask | null>(null);
  const [report, setReport] = useState<AtlasReport | null>(null);
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
    } catch (e) {
      setTask(null);
      setReport(null);
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    document.title = "Atlas · 任务工作台";
    load(DEFAULT_SESSION);
  }, [load]);

  return (
    <div className="app">
      <header>
        <h1>Atlas · 任务工作台</h1>
        <p className="muted">
          只读视图：任务状态、角色步骤与图节点来自 Atlas API（
          <code>scripts/serve_atlas_api.py</code>）。研究问题的检索与证据仍走{" "}
          <a href="/">CompassRAG 工作台</a>。
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
        {error && <p className="error">加载失败：{error}</p>}
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
                    <td>{node.content}</td>
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
