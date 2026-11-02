import { useState } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { api } from "../api";
import { usePassageViewer } from "../components/Evidence";
import { Card, Chip, Empty, ErrorBox, Loading, Notice, StatusChip, Tabs } from "../components/ui";
import { fmtTime } from "../format";
import { useAction, useAsync } from "../hooks";
import type { RelSide, Relationship } from "../types";

type Ctx = { ws: string };

const PREDICATE_LABEL: Record<string, string> = {
  supports: "supports",
  contradicts: "contradicts",
  extends: "extends",
  related: "is related to",
};

function Side({ s, label }: { s: RelSide; label: string }) {
  const viewer = usePassageViewer();
  return (
    <div className="side">
      <div className="small strong">
        {label}: {s.source_title}
        {s.page ? `, p.${s.page}` : ""}
      </div>
      <div className="small">{s.statement}</div>
      <button className="btn small" onClick={() => viewer.open(s.passage_id, s.quote)}>
        Open passage
      </button>
    </div>
  );
}

function RelationshipCard({ r, onChanged, ws }: { r: Relationship; onChanged: () => void; ws: string }) {
  const set = useAction((status: string) => api.setRelationship(ws, r.id, status));
  const change = async (status: string) => {
    if (await set.run(status)) onChanged();
  };
  return (
    <div className="ptext">
      <div className="row gap wrap small">
        <StatusChip status={r.status} label={r.status === "hypothesis" ? "hypothesis (unverified)" : r.status} />
        <Chip tone={r.predicate === "contradicts" ? "bad" : "info"}>{PREDICATE_LABEL[r.predicate] ?? r.predicate}</Chip>
        <Chip title="Who or what proposed this link">detected by {r.detected_by}</Chip>
      </div>
      <div className="sides">
        <Side s={r.a} label="A" />
        <Side s={r.b} label="B" />
      </div>
      {r.rationale && <div className="small muted">{r.rationale}</div>}
      <div className="row gap small">
        {r.status !== "supported" && <button className="btn small" disabled={set.busy} onClick={() => change("supported")}>Confirm</button>}
        {r.status !== "rejected" && <button className="btn small" disabled={set.busy} onClick={() => change("rejected")}>Reject</button>}
        {r.status !== "hypothesis" && <button className="btn small" disabled={set.busy} onClick={() => change("hypothesis")}>Mark as hypothesis</button>}
      </div>
      <ErrorBox error={set.error} />
    </div>
  );
}

function Consolidated({ ws }: { ws: string }) {
  const view = useAsync(() => api.consolidated(ws), [ws]);
  const run = useAction(() => api.consolidate(ws));
  const [queued, setQueued] = useState<string | null>(null);
  return (
    <Card
      title="Hindsight consolidated memory"
      actions={
        <button
          className="btn small"
          disabled={run.busy}
          onClick={async () => {
            const r = await run.run();
            if (r) setQueued(r.execution_id);
          }}
        >
          {run.busy ? "Queuing…" : "Refresh consolidation"}
        </button>
      }
    >
      {view.loading && !view.data && <Loading />}
      <ErrorBox error={view.error} onRetry={view.reload} />
      {queued && (
        <Notice tone="info">
          Consolidation queued. <Link to={`../inspector?exec=${queued}`}>Inspect run</Link>, then reload this panel.
        </Notice>
      )}
      <ErrorBox error={run.error} />
      {view.data && (
        <div className="stack">
          <Notice tone="warn">{view.data.disclaimer}</Notice>
          {!view.data.available && <div className="muted small">No consolidated view exists yet for this workspace.</div>}
          {view.data.view && (
            <>
              <div className="row gap small">
                {view.data.view.is_stale && <Chip tone="warn">stale</Chip>}
                <span className="muted">refreshed {fmtTime(view.data.view.last_refreshed_at)}</span>
              </div>
              <div className="consolidated">{view.data.view.content}</div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function MemoryOps({ ws }: { ws: string }) {
  const ops = useAsync(() => api.memoryOps(ws), [ws]);
  return (
    <Card title="Memory operations (ledger)" actions={<button className="btn small" onClick={ops.reload}>Refresh</button>}>
      <p className="muted small">Every Hindsight call (bank setup, retain writes and recalls) is recorded here. Retries never repeat a completed write.</p>
      {ops.loading && !ops.data && <Loading />}
      <ErrorBox error={ops.error} onRetry={ops.reload} />
      {ops.data?.length === 0 && <Empty title="No memory operations yet" />}
      {ops.data && ops.data.length > 0 && (
        <table className="table small">
          <thead>
            <tr><th>when</th><th>op</th><th>status</th><th>duration</th><th>run</th></tr>
          </thead>
          <tbody>
            {ops.data.map((o) => (
              <tr key={o.id}>
                <td>{fmtTime(o.created_at)}</td>
                <td><code>{o.op}</code></td>
                <td><StatusChip status={o.status === "done" || o.status === "ok" ? "succeeded" : o.status} label={o.status} /></td>
                <td>{o.duration_ms != null ? `${o.duration_ms} ms` : "—"}</td>
                <td>{o.execution_id ? <Link to={`../inspector?exec=${o.execution_id}`}>{o.execution_id}</Link> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

export function Knowledge() {
  const { ws } = useOutletContext<Ctx>();
  const [tab, setTab] = useState<"relationships" | "concepts" | "memory">("relationships");
  const k = useAsync(() => api.knowledge(ws), [ws]);
  const synth = useAction(() => api.synthesize(ws));
  const [queued, setQueued] = useState<string | null>(null);

  return (
    <div className="stack">
      <Card
        title="Knowledge explorer"
        actions={
          <button
            className="btn small"
            disabled={synth.busy}
            onClick={async () => {
              const r = await synth.run();
              if (r) setQueued(r.execution_id);
            }}
          >
            {synth.busy ? "Queuing…" : "Find cross-source relationships"}
          </button>
        }
      >
        {k.data && <div className="small muted">{k.data.stats.findings} findings from {k.data.stats.sources} source(s) · {k.data.stats.relationships} relationship(s)</div>}
        {k.data && <Notice tone="info">{k.data.stats.note}</Notice>}
        {queued && (
          <Notice tone="info">
            Synthesis queued. <Link to={`../inspector?exec=${queued}`}>Inspect run</Link>, then <button className="btn small" onClick={() => { k.reload(); setQueued(null); }}>reload</button>
          </Notice>
        )}
        <ErrorBox error={synth.error} />
        <Tabs
          tabs={[
            { id: "relationships", label: "Relationships", badge: k.data ? <Chip>{k.data.relationships.length}</Chip> : null },
            { id: "concepts", label: "Concepts", badge: k.data ? <Chip>{k.data.concepts.length}</Chip> : null },
            { id: "memory", label: "Memory" },
          ]}
          value={tab}
          onChange={setTab}
        />
      </Card>
      {tab !== "memory" && k.loading && !k.data && <Loading />}
      {tab !== "memory" && <ErrorBox error={k.error} onRetry={k.reload} />}

      {tab === "relationships" && k.data && (
        <div className="stack">
          {k.data.relationships.length === 0 && <Empty title="No relationships found">Add at least two sources, then run cross-source analysis.</Empty>}
          {k.data.relationships.map((r) => <RelationshipCard key={r.id} r={r} ws={ws} onChanged={k.reload} />)}
        </div>
      )}
      {tab === "concepts" && k.data && (
        <Card>
          {k.data.concepts.length === 0 && <Empty title="No concepts yet" />}
          <ul className="list">
            {k.data.concepts.map((c) => (
              <li key={c.name} className="listitem">
                <div className="row gap spread">
                  <strong>{c.name}</strong>
                  <span className="small muted">{c.finding_count} finding(s) · {c.source_count} source(s)</span>
                </div>
                <div className="row gap wrap small">
                  {c.sources.map((s) => (
                    <Link key={s.id} to={`../sources?open=${s.id}`} className="chip">{s.title || s.id}</Link>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {tab === "memory" && (
        <div className="stack">
          <Consolidated ws={ws} />
          <MemoryOps ws={ws} />
        </div>
      )}
    </div>
  );
}
