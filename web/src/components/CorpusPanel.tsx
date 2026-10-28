import { useState } from "react";
import type { DocumentMeta, IndexStatus } from "../api/client";

export function CorpusPanel(props: {
  status: IndexStatus | null;
  documents: DocumentMeta[];
  onUpload: (payload: { name: string; content: string; format: string }) => Promise<string>;
  onBuild: () => Promise<string>;
}) {
  const [name, setName] = useState("notes.txt");
  const [format, setFormat] = useState("txt");
  const [content, setContent] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<string>) => {
    setBusy(true);
    setMessage(null);
    try {
      setMessage(await fn());
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel corpus-panel">
      <h2>语料与索引</h2>
      {props.status ? (
        <dl className="meta">
          <div>
            <dt>文档</dt>
            <dd>{props.status.n_documents ?? 0}</dd>
          </div>
          <div>
            <dt>块</dt>
            <dd>{props.status.n_chunks ?? 0}</dd>
          </div>
          <div>
            <dt>运行时版本</dt>
            <dd>{props.status.runtime_version || "—"}</dd>
          </div>
          <div>
            <dt>最新构建</dt>
            <dd>{props.status.built_version || "—"}</dd>
          </div>
        </dl>
      ) : (
        <p className="muted">未启用语料管理（后端未挂载 ingest）</p>
      )}
      {props.status?.mismatch && (
        <p className="hint">⚠ 运行时索引与最新构建不是同一版：数字与回答可能对不上。</p>
      )}
      {props.status?.last_error && <p className="hint">最近一次构建错误：{props.status.last_error}</p>}

      <div className="corpus-form">
        <div className="controls">
          <label>
            文件名
            <input aria-label="文档名" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            格式
            <select aria-label="文档格式" value={format} onChange={(e) => setFormat(e.target.value)}>
              <option value="txt">txt</option>
              <option value="md">md</option>
              <option value="json">json</option>
            </select>
          </label>
        </div>
        <textarea
          aria-label="文档内容"
          rows={3}
          value={content}
          placeholder="粘贴文档内容（json 需 {title, text} 或数组）"
          onChange={(e) => setContent(e.target.value)}
        />
        <div className="controls">
          <button
            disabled={busy || !content.trim()}
            onClick={() =>
              run(async () => {
                const r = await props.onUpload({ name, content, format });
                setContent("");
                return r;
              })
            }
          >
            上传文档
          </button>
          <button className="ghost" disabled={busy} onClick={() => run(props.onBuild)}>
            重建索引
          </button>
          <span className="muted small">已收录 {props.documents.length} 个文档</span>
        </div>
        {message && <p className="hint">{message}</p>}
      </div>
    </section>
  );
}
