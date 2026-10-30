import { useState } from "react";

export interface RunOptions {
  max_rounds: number;
  budget_tokens: number;
}

export function ResearchInput(props: {
  running: boolean;
  onSubmit: (query: string, options: RunOptions) => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState("");
  const [maxRounds, setMaxRounds] = useState(2);
  const [budget, setBudget] = useState(4000);

  const submit = () => {
    const trimmed = query.trim();
    if (!trimmed || props.running) return;
    props.onSubmit(trimmed, { max_rounds: maxRounds, budget_tokens: budget });
  };

  return (
    <section className="panel input-panel">
      <h2>提问</h2>
      <textarea
        aria-label="研究问题"
        rows={4}
        value={query}
        placeholder="例如：Who wrote the novel that the film Blade Runner was based on?"
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="controls">
        <label>
          最大轮数
          <input
            aria-label="最大轮数"
            type="number"
            min={1}
            max={5}
            value={maxRounds}
            onChange={(e) => setMaxRounds(Number(e.target.value))}
          />
        </label>
        <label>
          token 预算
          <input
            aria-label="token 预算"
            type="number"
            min={0}
            step={500}
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
          />
        </label>
        <button onClick={submit} disabled={props.running || !query.trim()}>
          {props.running ? "运行中…" : "开始检索"}
        </button>
        <button className="ghost" onClick={props.onCancel} disabled={!props.running}>
          停止
        </button>
      </div>
    </section>
  );
}
