import { useState } from "react";
import { api } from "../api";
import { Card, Chip, Empty, ErrorBox, JsonBlock, Loading, Notice } from "../components/ui";
import { fmtTime } from "../format";
import { useAction, useAsync } from "../hooks";
import type { EvalRun } from "../types";

function RunView({ run }: { run: EvalRun }) {
  return (
    <div className="stack">
      <div className="row gap wrap small">
        <Chip tone={run.passed ? "good" : "bad"}>{run.passed ? "PASSED" : "FAILED"}</Chip>
        <Chip>{run.environment.generation_mode} mode</Chip>
        <Chip tone={run.environment.hindsight_live ? "good" : "warn"}>{run.environment.hindsight_live ? "live Hindsight" : "Hindsight not live"}</Chip>
        <span className="muted">{fmtTime(run.started_at)}</span>
      </div>
      {run.environment.notes.map((n, i) => <Notice key={i} tone="warn">{n}</Notice>)}
      <table className="table">
        <thead>
          <tr><th>gate</th><th>value</th><th>need</th><th>result</th></tr>
        </thead>
        <tbody>
          {run.gates.map((g) => (
            <tr key={g.name} className={g.passed === false ? "row-bad" : ""}>
              <td>{g.name}</td>
              <td>{g.value == null ? "—" : g.value.toFixed(3)}</td>
              <td>{g.op}{g.threshold}</td>
              <td>
                {g.skipped ? <Chip tone="warn" title={g.reason}>skipped</Chip> : <Chip tone={g.passed ? "good" : "bad"}>{g.passed ? "PASS" : "FAIL"}</Chip>}
                {g.skipped && g.reason && <span className="small muted"> {g.reason}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <details>
        <summary className="small strong">All metrics</summary>
        <JsonBlock value={run.metrics} />
      </details>
    </div>
  );
}

export function Evaluation() {
  const runs = useAsync(api.evaluations, []);
  const start = useAction(api.runEvaluation);
  const [note, setNote] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const current = runs.data?.find((r) => r.id === sel) ?? runs.data?.[0];

  return (
    <div className="stack">
      <h1>Evaluation</h1>
      <Notice tone="info">
        The deterministic suite runs a fixed corpus in an isolated sandbox and checks retrieval, citation validity, contradiction detection, summary quality, injection resistance and workspace isolation. Results reflect the mode shown on each run; extractive-mode numbers say nothing about LLM quality.
      </Notice>
      <div className="row gap">
        <button
          className="btn primary"
          disabled={start.busy}
          onClick={async () => {
            const r = await start.run();
            if (r) setNote(`${r.note} (execution ${r.execution_id}) Refresh in a few minutes.`);
          }}
        >
          {start.busy ? "Queuing…" : "Run evaluation"}
        </button>
        <button className="btn" onClick={runs.reload}>Refresh</button>
      </div>
      {note && <Notice tone="info">{note}</Notice>}
      <ErrorBox error={start.error} />
      {runs.loading && !runs.data && <Loading />}
      <ErrorBox error={runs.error} onRetry={runs.reload} />
      {runs.data?.length === 0 && <Empty title="No evaluation runs yet">Run <code>python -m evaldata.run</code> or use the button above.</Empty>}
      <div className="split">
        <Card title="Runs">
          <ul className="list">
            {runs.data?.map((r) => (
              <li key={r.id} className={r.id === current?.id ? "listitem active" : "listitem"} tabIndex={0} onClick={() => setSel(r.id)} onKeyDown={(e) => e.key === "Enter" && setSel(r.id)}>
                <div className="row gap spread">
                  <span>{fmtTime(r.started_at)}</span>
                  <Chip tone={r.passed ? "good" : r.passed === false ? "bad" : "neutral"}>{r.passed ? "passed" : r.passed === false ? "failed" : "running"}</Chip>
                </div>
              </li>
            ))}
          </ul>
        </Card>
        {current && <Card title="Gates"><RunView run={current} /></Card>}
      </div>
    </div>
  );
}
