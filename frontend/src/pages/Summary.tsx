import { useState } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { api } from "../api";
import { CitationChip } from "../components/Evidence";
import { Card, Chip, Empty, ErrorBox, Loading, Notice, StatusChip } from "../components/ui";
import { fmtTime, SECTION_TITLES } from "../format";
import { useAction, useAsync } from "../hooks";
import type { SummaryDiff, SummaryItem } from "../types";

type Ctx = { ws: string; refreshWorkspace: () => void };
const STATUS_HINT: Record<string, string> = {
  new: "First appeared in this version",
  established: "Carried over from earlier versions",
  contested: "Sources disagree about this",
  hypothesis: "An unverified inference, not stated by a source",
};

function Item({ item }: { item: SummaryItem }) {
  return (
    <li className="sumitem">
      <div>{item.text}</div>
      <div className="row gap wrap small">
        <span title={STATUS_HINT[item.status]}>
          <StatusChip status={item.status} />
        </span>
        {item.citations.map((c, i) => (
          <CitationChip key={i} c={{ ...c, verified: true }} />
        ))}
        {item.citations.length === 0 && <Chip tone="warn">no citation</Chip>}
      </div>
    </li>
  );
}

function DiffView({ diff }: { diff: SummaryDiff }) {
  if (!diff.meaningful) return <Notice tone="info">No meaningful difference between v{diff.from} and v{diff.to}.</Notice>;
  return (
    <div className="stack">
      <div className="small muted">
        v{diff.from} → v{diff.to} · {diff.unchanged} item(s) unchanged
      </div>
      {diff.added.map((a) => (
        <div key={a.key} className="diff add"><strong>+ {SECTION_TITLES[a.section] ?? a.section}</strong> {a.text}</div>
      ))}
      {diff.removed.map((r) => (
        <div key={r.key} className="diff del"><strong>− {SECTION_TITLES[r.section] ?? r.section}</strong> {r.text} <span className="muted">({r.reason})</span></div>
      ))}
      {diff.changed.map((c) => (
        <div key={c.key} className="diff chg">
          <strong>~ {SECTION_TITLES[c.section] ?? c.section}</strong>
          <div className="del">{c.before}</div>
          <div className="add">{c.after}</div>
        </div>
      ))}
    </div>
  );
}

export function Summary() {
  const { ws, refreshWorkspace } = useOutletContext<Ctx>();
  const cur = useAsync(() => api.summary(ws), [ws]);
  const versions = useAsync(() => api.summaryVersions(ws), [ws]);
  const [viewing, setViewing] = useState<number | null>(null);
  const [diffTo, setDiffTo] = useState<{ from: number; to: number } | null>(null);
  const shown = useAsync(() => (viewing ? api.summaryVersion(ws, viewing) : Promise.resolve(undefined)), [ws, viewing]);
  const diff = useAsync(() => (diffTo ? api.summaryDiff(ws, diffTo.from, diffTo.to) : Promise.resolve(undefined)), [ws, diffTo?.from, diffTo?.to]);
  const update = useAction(() => api.updateSummary(ws));
  const rollback = useAction((v: number) => api.rollbackSummary(ws, v));
  const [outcome, setOutcome] = useState<{ tone: "good" | "info" | "bad" | "warn"; text: string; exec: string } | null>(null);

  const refresh = () => {
    refreshWorkspace(); // header chip shows the current summary version
    cur.reload();
    versions.reload();
    setViewing(null);
  };
  async function doUpdate() {
    const r = await update.run();
    if (!r) return;
    if (r.status !== "succeeded") setOutcome({ tone: "bad", text: `Update failed — the last valid version was kept. ${r.error ?? ""}`, exec: r.execution_id });
    else if (r.result?.created_version) {
      const more = r.result.deferred_items ?? 0;
      setOutcome({
        tone: more ? "warn" : "good",
        text: `Created version ${r.result.created_version}.` + (more ? ` ${more} more item(s) were deferred by the per-update cap — press Update again to include them.` : ""),
        exec: r.execution_id,
      });
    }
    else setOutcome({ tone: "info", text: "No new information since the last version — nothing was created.", exec: r.execution_id });
    refresh();
  }
  async function doRollback(v: number) {
    if (!window.confirm(`Create a new version whose content is a copy of version ${v}? History is kept.`)) return;
    const r = await rollback.run(v);
    if (!r) return;
    setOutcome(
      r.status === "succeeded"
        ? { tone: "good", text: `Rolled back by creating version ${r.result?.created_version ?? "?"}.`, exec: r.execution_id }
        : { tone: "bad", text: `Rollback failed. ${r.error ?? ""}`, exec: r.execution_id },
    );
    refresh();
  }

  const view = viewing ? shown.data : cur.data;
  const latest = cur.data?.version ?? 0;
  return (
    <div className="split">
      <div className="stack">
        <Card
          title={view?.version ? `Living summary — v${view.version}${viewing && viewing !== latest ? " (older version)" : ""}` : "Living summary"}
          actions={
            <button className="btn primary small" disabled={update.busy} onClick={doUpdate}>
              {update.busy ? "Updating…" : "Update from new evidence"}
            </button>
          }
        >
          {(cur.loading && !cur.data) || (viewing && shown.loading && !shown.data) ? <Loading /> : null}
          <ErrorBox error={cur.error ?? shown.error} onRetry={refresh} />
          <ErrorBox error={update.error ?? rollback.error} />
          {outcome && (
            <Notice tone={outcome.tone}>
              {outcome.text} <Link to={`../inspector?exec=${outcome.exec}`}>Inspect run</Link>
            </Notice>
          )}
          {view && view.version === 0 && <Empty title="No summary yet">{view.message}</Empty>}
          {view?.content && (
            <div className="stack">
              {view.change?.summary && <Notice tone="info">{view.change.summary}</Notice>}
              <p>{view.content.overview}</p>
              {view.content.overview_mode && <div className="small muted">Overview written in {view.content.overview_mode} mode.</div>}
              {Object.entries(view.content.sections)
                .filter(([, items]) => items.length > 0)
                .map(([key, items]) => (
                  <section key={key}>
                    <h3>{SECTION_TITLES[key] ?? key}</h3>
                    <ul className="sumlist">{items.map((it) => <Item key={it.key} item={it} />)}</ul>
                  </section>
                ))}
              <div className="small muted">
                Every item cites stored source passages. Items marked <em>hypothesis</em> are inferences and are never presented as quotations.
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="stack">
        <Card title="Version history">
          {versions.loading && !versions.data && <Loading />}
          <ErrorBox error={versions.error} onRetry={versions.reload} />
          {versions.data?.length === 0 && <div className="muted small">No versions yet.</div>}
          <ul className="list">
            {versions.data?.map((v) => (
              <li key={v.version} className={v.version === (viewing ?? latest) ? "listitem active" : "listitem"}>
                <div className="row gap spread">
                  <strong>v{v.version}</strong>
                  <span className="small muted">{v.kind}{v.parent_version ? ` · from v${v.parent_version}` : ""} · {fmtTime(v.created_at)}</span>
                </div>
                <div className="small">{v.summary ?? "—"} · {v.sources} source(s)</div>
                <div className="row gap small">
                  <button className="btn small" onClick={() => setViewing(v.version === latest ? null : v.version)}>View</button>
                  {v.parent_version != null && <button className="btn small" onClick={() => setDiffTo({ from: v.parent_version!, to: v.version })}>Diff vs parent</button>}
                  {v.version !== latest && <button className="btn small" disabled={rollback.busy} onClick={() => doRollback(v.version)}>Roll back to this</button>}
                </div>
              </li>
            ))}
          </ul>
        </Card>
        {diffTo && (
          <Card title="Changes" actions={<button className="btn small" onClick={() => setDiffTo(null)}>Close</button>}>
            {diff.loading && !diff.data && <Loading />}
            <ErrorBox error={diff.error} />
            {diff.data && <DiffView diff={diff.data} />}
          </Card>
        )}
      </div>
    </div>
  );
}
