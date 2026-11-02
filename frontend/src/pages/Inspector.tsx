import { useEffect } from "react";
import { useOutletContext, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ExecutionHeader, StateMachine, StepTimeline } from "../components/Execution";
import { Card, Chip, Empty, ErrorBox, JsonBlock, Loading, Notice, StatusChip } from "../components/ui";
import { fmtMs, relTime } from "../format";
import { useAction, useAsync } from "../hooks";

type Ctx = { ws: string };
const ACTIVE = new Set(["queued", "running"]);

function ExecutionDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const ex = useAsync(() => api.execution(id), [id], (d) => (d && ACTIVE.has(d.status) ? 2000 : null));
  const defs = useAsync(api.workflows, []);
  const resume = useAction(() => api.resume(id));
  if (ex.error) return <ErrorBox error={ex.error} onRetry={ex.reload} />;
  if (!ex.data) return <Loading />;
  const e = ex.data;
  const def = defs.data?.find((d) => d.name === e.workflow);
  return (
    <div className="stack">
      <ExecutionHeader ex={e} />
      <StateMachine def={def} steps={e.steps ?? []} failedStage={e.failed_stage} currentStage={e.current_stage} />
      {e.error && (
        <div className="errorbox">
          <strong>Failed{e.failed_stage ? ` at ${e.failed_stage}` : ""}:</strong> {e.error}
        </div>
      )}
      {(e.status === "failed" || e.status === "interrupted") && (
        <div className="row gap">
          <button
            className="btn primary"
            disabled={resume.busy}
            onClick={async () => {
              if (await resume.run()) {
                ex.reload();
                onChanged();
              }
            }}
          >
            {resume.busy ? "Resuming…" : "Resume from the failed stage"}
          </button>
          <span className="muted small">Completed stages and side effects (memory writes) are not repeated.</span>
        </div>
      )}
      <ErrorBox error={resume.error} />
      <StepTimeline steps={e.steps ?? []} />
      {e.result && Object.keys(e.result).length > 0 && (
        <details>
          <summary className="small strong">Result</summary>
          <JsonBlock value={e.result} />
        </details>
      )}
    </div>
  );
}

export function Inspector() {
  const { ws } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const sel = params.get("exec");
  const status = params.get("status") ?? "";
  const workflow = params.get("workflow") ?? "";
  const list = useAsync(
    () => api.executions({ workspace_id: ws, ...(status ? { status } : {}), ...(workflow ? { workflow } : {}) }),
    [ws, status, workflow],
    (d) => (d?.some((x) => ACTIVE.has(x.status)) ? 3000 : 15000),
  );
  const patch = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) (v ? next.set(k, v) : next.delete(k));
    setParams(next, { replace: true });
  };
  // options come from the declared workflows, not from the (already filtered) result list, so a filter can always be changed
  const defs = useAsync(api.workflows, []);
  useEffect(() => {
    // On narrow screens the detail card sits below a long list; bring it into view when a run is selected.
    if (sel && window.matchMedia?.("(max-width: 860px)").matches) document.querySelector(".exec-detail")?.scrollIntoView?.({ block: "start" });
  }, [sel]);
  const workflows = defs.data?.map((d) => d.name) ?? [...new Set(list.data?.map((x) => x.workflow) ?? [])];

  return (
    <div className="split">
      <Card title="Workflow executions" actions={<button className="btn small" onClick={list.reload}>Refresh</button>}>
        <p className="muted small">Every ingestion, answer, synthesis and summary run is persisted here with its stages, checkpoints and evidence.</p>
        <div className="row gap wrap">
          <select aria-label="Filter by status" value={status} onChange={(e) => patch({ status: e.target.value || null })}>
            <option value="">all statuses</option>
            {["queued", "running", "succeeded", "failed", "interrupted"].map((s) => <option key={s}>{s}</option>)}
          </select>
          <select aria-label="Filter by workflow" value={workflow} onChange={(e) => patch({ workflow: e.target.value || null })}>
            <option value="">all workflows</option>
            {workflows.map((w) => <option key={w}>{w}</option>)}
          </select>
        </div>
        {list.loading && !list.data && <Loading />}
        <ErrorBox error={list.error} onRetry={list.reload} />
        {list.data?.length === 0 && <Empty title="No executions match" />}
        <ul className="list">
          {list.data?.map((x) => (
            <li key={x.id} className={x.id === sel ? "listitem active" : "listitem"} tabIndex={0} onClick={() => patch({ exec: x.id })} onKeyDown={(e) => e.key === "Enter" && patch({ exec: x.id })}>
              <div className="row gap spread">
                <div>
                  <strong>{x.workflow}</strong> <code className="small">{x.id}</code>
                  <div className="small muted">
                    {relTime(x.created_at)} · {fmtMs(x.duration_ms)}
                    {x.failed_stage ? ` · failed at ${x.failed_stage}` : x.current_stage && x.status === "running" ? ` · ${x.current_stage}` : ""}
                  </div>
                </div>
                <StatusChip status={x.status} />
              </div>
            </li>
          ))}
        </ul>
      </Card>
      <Card title={sel ? "Execution detail" : "Select an execution"} className="exec-detail">
        {sel ? <ExecutionDetail id={sel} onChanged={list.reload} /> : <Notice tone="info">Pick a run on the left to see its state machine, steps, retrieved evidence and validations.</Notice>}
        {sel && (
          <div className="row gap small">
            <Chip>{sel}</Chip>
            <button className="btn small" onClick={() => patch({ exec: null })}>Close</button>
          </div>
        )}
      </Card>
    </div>
  );
}
