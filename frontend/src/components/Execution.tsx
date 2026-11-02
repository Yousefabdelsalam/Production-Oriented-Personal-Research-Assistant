import { Fragment, useState } from "react";
import { fmtMs, fmtTime } from "../format";
import type { Execution, Step, WorkflowDef } from "../types";
import { Chip, JsonBlock, StatusChip } from "./ui";

/** The workflow's declared state machine with the execution's actual progress painted on it. */
export function StateMachine({ def, steps, failedStage, currentStage }: { def?: WorkflowDef; steps: Step[]; failedStage: string | null; currentStage: string | null }) {
  if (!def) return null;
  const status: Record<string, string> = {};
  for (const s of steps) status[s.stage] = s.status === "succeeded" ? "succeeded" : s.status === "failed" ? "failed" : status[s.stage] ?? s.status;
  return (
    <div className="machine" aria-label="Workflow state machine">
      {def.stages.map((st, i) => {
        const s = status[st.name] ?? (failedStage === st.name ? "failed" : currentStage === st.name ? "running" : "pending");
        const branches = st.next.length > 1;
        return (
          <div key={st.name} className={`node node-${s}`} title={`→ ${st.next.join(" | ") || "end"}${st.retries ? ` · up to ${st.retries} retries` : ""}`}>
            <span className="idx">{i + 1}</span>
            {st.name.replaceAll("_", " ").toLowerCase()}
            {branches && <span className="branch"> ⑂</span>}
          </div>
        );
      })}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="stepsec">
      <div className="small strong">{title}</div>
      {children}
    </div>
  );
}

function StepDetail({ step }: { step: Step }) {
  const d = step.detail as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const known = new Set(["retrieved", "validations", "plan", "memory", "model_calls", "retention_policy", "next"]);
  const rest = Object.fromEntries(Object.entries(d).filter(([k]) => !known.has(k)));
  return (
    <div className="stack">
      {step.error && <div className="errorbox">{step.error}</div>}
      {d.plan && (
        <Section title="Plan (what the harness decided to fetch, and why)">
          <JsonBlock value={d.plan} />
        </Section>
      )}
      {Array.isArray(d.retrieved) && (
        <Section title={`Retrieved evidence (${d.retrieved.length}) — and why each was considered relevant`}>
          <table className="table small">
            <thead>
              <tr><th>passage</th><th>source</th><th>page</th><th>score</th><th>why</th></tr>
            </thead>
            <tbody>
              {d.retrieved.map((r: any, i: number) => ( // eslint-disable-line @typescript-eslint/no-explicit-any
                <tr key={i}>
                  <td><code>{r.passage}</code></td><td><code>{r.source}</code></td><td>{r.page ?? "—"}</td><td>{r.score}</td>
                  <td>{(r.why ?? []).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
      {Array.isArray(d.validations) && (
        <Section title="Validations">
          <ul className="vals">
            {d.validations.map((v: any, i: number) => ( // eslint-disable-line @typescript-eslint/no-explicit-any
              <li key={i}>
                <Chip tone={v.passed ? "good" : "bad"}>{v.passed ? "pass" : "fail"}</Chip> <strong>{v.name}</strong>
                <details><summary className="muted small">details</summary><JsonBlock value={v.info} /></details>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {d.memory && (
        <Section title="Memory (Hindsight)">
          <JsonBlock value={d.memory} />
        </Section>
      )}
      {d.retention_policy && (
        <Section title="Retention policy decision">
          <JsonBlock value={d.retention_policy} />
        </Section>
      )}
      {Array.isArray(d.model_calls) && d.model_calls.length > 0 && (
        <Section title="Model calls">
          <JsonBlock value={d.model_calls} />
        </Section>
      )}
      {Object.keys(rest).length > 0 && (
        <details>
          <summary className="small strong">Other trace data</summary>
          <JsonBlock value={rest} />
        </details>
      )}
    </div>
  );
}

export function StepTimeline({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <div className="table-scroll">
    <table className="table">
      <thead>
        <tr><th>#</th><th>stage</th><th>attempt</th><th>status</th><th>duration</th><th>started</th><th /></tr>
      </thead>
      <tbody>
        {steps.map((s, i) => (
          <Fragment key={s.id}>
            <tr className={s.status === "failed" ? "row-bad" : ""}>
              <td>{i + 1}</td>
              <td><code>{s.stage}</code></td>
              <td>{s.attempt}</td>
              <td><StatusChip status={s.status} /></td>
              <td>{fmtMs(s.duration_ms)}</td>
              <td className="muted small nowrap">{fmtTime(s.started_at)}</td>
              <td><button className="btn small" onClick={() => setOpen(open === s.id ? null : s.id)}>{open === s.id ? "Hide" : "Details"}</button></td>
            </tr>
            {open === s.id && (
              <tr>
                <td colSpan={7}><StepDetail step={s} /></td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
    </div>
  );
}

export function ExecutionHeader({ ex }: { ex: Execution }) {
  return (
    <div className="kv">
      <div><span className="muted">Workflow</span><strong>{ex.workflow}</strong></div>
      <div><span className="muted">Status</span><StatusChip status={ex.status} /></div>
      <div><span className="muted">Correlation id</span><code>{ex.correlation_id}</code></div>
      <div><span className="muted">Duration</span>{fmtMs(ex.duration_ms)}</div>
      <div><span className="muted">Resumes</span>{ex.resume_count}</div>
      <div><span className="muted">Model calls</span>{ex.model_calls} ({ex.tokens_in}/{ex.tokens_out} tok)</div>
      <div><span className="muted">Trigger</span><code>{JSON.stringify(ex.trigger)}</code></div>
      <div><span className="muted">Started</span>{fmtTime(ex.started_at ?? ex.created_at)}</div>
    </div>
  );
}
