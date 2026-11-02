import { DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import { Link, useOutletContext, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, Drawer, Empty, ErrorBox, Loading, Notice, StatusChip, Tabs } from "../components/ui";
import { fmtTime, relTime } from "../format";
import { useAction, useAsync } from "../hooks";
import type { Source, SubmitResult } from "../types";

type Ctx = { ws: string; refreshWorkspace: () => void };
const KIND_ICON: Record<string, string> = { pdf: "PDF", url: "URL", note: "NOTE" };

function ResultNotice({ r }: { r: SubmitResult | undefined }) {
  if (!r) return null;
  const tone = r.duplicate ? "warn" : "good";
  return (
    <Notice tone={tone}>
      {r.duplicate ? "Already in this workspace — nothing was duplicated. " : ""}
      {r.message}
    </Notice>
  );
}

function AddPanel({ ws, onAdded }: { ws: string; onAdded: () => void }) {
  const [tab, setTab] = useState<"pdf" | "url" | "note">("pdf");
  const [result, setResult] = useState<SubmitResult | undefined>();
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [drag, setDrag] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const upload = useAction((f: File) => api.uploadPdf(ws, f));
  const addUrl = useAction((u: string) => api.addUrl(ws, u));
  const addNote = useAction((t: string, x: string) => api.addNote(ws, t, x));

  const done = (r?: SubmitResult) => {
    setResult(r);
    if (r) onAdded();
  };
  async function onFiles(files: FileList | null) {
    const f = files?.[0];
    if (f) done(await upload.run(f));
  }
  async function submitUrl(e: FormEvent) {
    e.preventDefault();
    const r = await addUrl.run(url.trim());
    if (r) setUrl("");
    done(r);
  }
  async function submitNote(e: FormEvent) {
    e.preventDefault();
    const r = await addNote.run(title.trim(), text);
    if (r) {
      setText("");
      setTitle("");
    }
    done(r);
  }
  const drop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    void onFiles(e.dataTransfer.files);
  };

  return (
    <Card title="Add a source">
      <Tabs tabs={[{ id: "pdf", label: "PDF" }, { id: "url", label: "URL" }, { id: "note", label: "Note" }]} value={tab} onChange={(t) => { setTab(t); setResult(undefined); }} />
      {tab === "pdf" && (
        <div className={drag ? "dropzone drag" : "dropzone"} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={drop}>
          <input ref={fileRef} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => onFiles(e.target.files)} aria-label="PDF file" />
          <p>Drop a research paper here, or</p>
          <button className="btn" disabled={upload.busy} onClick={() => fileRef.current?.click()}>{upload.busy ? "Uploading…" : "Choose a PDF"}</button>
          <p className="muted small">Text is extracted with page numbers. Scanned (image-only) PDFs are rejected clearly — OCR isn’t supported.</p>
          <ErrorBox error={upload.error} />
        </div>
      )}
      {tab === "url" && (
        <form className="stack" onSubmit={submitUrl}>
          <input type="url" aria-label="Article URL" placeholder="https://… (public articles & documentation)" value={url} onChange={(e) => setUrl(e.target.value)} required />
          <div className="row gap">
            <button className="btn primary" disabled={addUrl.busy || !url.trim()}>{addUrl.busy ? "Checking…" : "Add URL"}</button>
          </div>
          <p className="muted small">Private/internal addresses are blocked (SSRF protection). Login or paywalled pages are never bypassed.</p>
          <ErrorBox error={addUrl.error} />
        </form>
      )}
      {tab === "note" && (
        <form className="stack" onSubmit={submitNote}>
          <input aria-label="Note title" placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
          <textarea aria-label="Note text" rows={6} placeholder="Markdown or plain text — your beliefs, hypotheses, open questions…" value={text} onChange={(e) => setText(e.target.value)} required />
          <div className="row gap"><button className="btn primary" disabled={addNote.busy || !text.trim()}>{addNote.busy ? "Saving…" : "Save note"}</button></div>
          <ErrorBox error={addNote.error} />
        </form>
      )}
      <ResultNotice r={result} />
    </Card>
  );
}

function NoteEditor({ ws, source, onSaved }: { ws: string; source: Source; onSaved: () => void }) {
  const { data, loading } = useAsync(() => api.sourceText(ws, source.id), [ws, source.id, source.version]);
  const [title, setTitle] = useState(source.title);
  const [text, setText] = useState<string | null>(null);
  const save = useAction((t: string, x: string) => api.editNote(ws, source.id, t, x));
  const [msg, setMsg] = useState<SubmitResult | undefined>();
  if (loading) return <Loading />;
  const current = text ?? data?.text ?? "";
  async function submit() {
    const r = await save.run(title, current);
    if (r) {
      setMsg(r);
      onSaved();
    }
  }
  return (
    <div className="stack">
      <input aria-label="Note title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <textarea aria-label="Note text" rows={10} value={current} onChange={(e) => setText(e.target.value)} />
      <div className="row gap"><button className="btn primary" onClick={submit} disabled={save.busy}>{save.busy ? "Saving…" : "Save as new version"}</button></div>
      <p className="muted small">Editing creates a new version of this same note (no duplicate). Findings and memory from the previous version are superseded.</p>
      <ErrorBox error={save.error} />
      {msg && <Notice tone={msg.state === "unchanged" ? "info" : "good"}>{msg.message}</Notice>}
    </div>
  );
}

function SourceDrawerBody({ ws, id, onChanged }: { ws: string; id: string; onChanged: () => void }) {
  const [tab, setTab] = useState<"overview" | "text" | "findings" | "edit">("overview");
  // A resumed run flips only the execution to queued/running; the source still reads failed/partial until the worker claims it.
  const running = (d?: { status: string; latest_execution: { status: string } | null }) =>
    !!d && (d.status === "processing" || d.status === "queued" || ["queued", "running"].includes(d.latest_execution?.status ?? ""));
  const { data: s, error, reload } = useAsync(() => api.source(ws, id), [ws, id], (d) => (running(d) ? 2000 : null));
  const lastStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (lastStatus.current !== undefined && s && s.status !== lastStatus.current) onChanged(); // keep the library list in step with the drawer
    lastStatus.current = s?.status;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s?.status]);
  const text = useAsync(() => (tab === "text" ? api.sourcePassages(ws, id) : Promise.resolve(undefined)), [ws, id, tab, s?.version]);
  const findings = useAsync(() => (tab === "findings" ? api.sourceFindings(ws, id) : Promise.resolve(undefined)), [ws, id, tab, s?.version]);
  const retry = useAction(() => api.retrySource(ws, id));
  const refetch = useAction(() => api.addUrl(ws, s?.url ?? "", true));

  if (error) return <ErrorBox error={error} />;
  if (!s) return <Loading />;
  const ex = s.latest_execution;
  return (
    <div className="stack">
      <div className="row gap wrap">
        <StatusChip status={s.status} />
        <Chip>{KIND_ICON[s.kind]}</Chip>
        <Chip>version {s.version}</Chip>
        {s.duplicate_of && <Chip tone="warn">duplicate of <code>{s.duplicate_of}</code></Chip>}
        {s.published_at && <Chip>published {s.published_at.slice(0, 10)}</Chip>}
      </div>
      {s.url && <a href={s.url} target="_blank" rel="noreferrer noopener" className="small">{s.url}</a>}
      {s.error && <div className="errorbox"><strong>Last error{ex?.failed_stage ? ` (stage ${ex.failed_stage})` : ""}:</strong> {s.error}</div>}
      {(s.status === "failed" || s.status === "partial") && ex && running(s) && <Notice tone="info">Retry in progress: resuming from the failed stage…</Notice>}
      {(s.status === "failed" || s.status === "partial") && ex && !running(s) && (
        <div className="row gap">
          <button className="btn primary" disabled={retry.busy} onClick={async () => { if (await retry.run()) { reload(); onChanged(); } }}>
            {retry.busy ? "Resuming…" : "Retry from the failed stage"}
          </button>
          <span className="muted small">Completed stages and memory writes are not repeated.</span>
        </div>
      )}
      <ErrorBox error={retry.error} />
      {s.status === "partial" && <Notice tone="warn">Evidence is already searchable; a later stage (usually memory retention) failed. The source is not marked complete until it succeeds.</Notice>}
      {s.flagged_passages > 0 && <Notice tone="warn">{s.flagged_passages} passage(s) contain instruction-like text. They are stored and shown as data only; they are excluded from findings and memory and nothing in them is ever executed.</Notice>}

      <Tabs tabs={[{ id: "overview", label: "Overview" }, { id: "text", label: "Extracted text" }, { id: "findings", label: "Findings", badge: <Chip>{s.findings.validated}</Chip> }, ...(s.kind === "note" ? [{ id: "edit" as const, label: "Edit" }] : [])]} value={tab} onChange={setTab} />

      {tab === "overview" && (
        <div className="stack">
          <div className="kv">
            <div><span className="muted">Passages</span>{s.passages}</div>
            <div><span className="muted">Findings validated / rejected</span>{s.findings.validated} / {s.findings.rejected}</div>
            <div><span className="muted">Findings in memory</span>{s.findings.retained}</div>
            <div><span className="muted">Added</span>{fmtTime(s.created_at)}</div>
            <div><span className="muted">Last stage</span><code>{s.last_stage ?? "—"}</code></div>
            {ex && <div><span className="muted">Ingestion run</span><Link to={`../inspector?exec=${ex.id}`}>{ex.id}</Link> <StatusChip status={ex.status} /></div>}
          </div>
          {s.versions.map((v) => (
            <div key={v.version} className="small muted">
              v{v.version}: {v.chars.toLocaleString()} chars · {(v.size_bytes / 1024).toFixed(0)} KB original ({v.mime ?? "n/a"}) · sha {v.content_hash} · {fmtTime(v.created_at)}
              {Boolean(v.meta.page_count) && <> · {String(v.meta.page_count)} pages</>}
              {Boolean(v.meta.author) && <> · {String(v.meta.author)}</>}
            </div>
          ))}
          {s.kind === "url" && s.status !== "processing" && (
            <div className="row gap">
              <button className="btn small" disabled={refetch.busy} onClick={async () => { if (await refetch.run()) { reload(); onChanged(); } }}>Re-fetch (detects changes)</button>
            </div>
          )}
        </div>
      )}
      {tab === "text" && (
        <div className="stack">
          {text.loading && <Loading />}
          <ErrorBox error={text.error} />
          {text.data?.length === 0 && <Empty title="No extracted text yet" />}
          {text.data?.map((p) => (
            <div key={p.id} className={p.suspicious ? "ptext flagged" : "ptext"}>
              <div className="small muted">
                {p.page_start ? `p.${p.page_start}` : "—"}{p.section ? ` · ${p.section}` : ""} · <code>{p.id}</code>
                {p.suspicious && <Chip tone="warn">instruction-like text</Chip>}
              </div>
              {p.text}
            </div>
          ))}
        </div>
      )}
      {tab === "findings" && (
        <div className="stack">
          {findings.loading && <Loading />}
          <ErrorBox error={findings.error} />
          {findings.data?.map((f) => (
            <div key={f.id} className="ptext">
              <div className="row gap wrap small">
                <Chip tone={f.validated ? "good" : "bad"}>{f.validated ? "validated" : "rejected"}</Chip>
                <Chip>{f.kind}</Chip>
                <Chip tone={f.epistemic === "user_note" ? "accent" : "info"}>{f.epistemic.replace("_", " ")}</Chip>
                <Chip>memory: {f.memory_state}</Chip>
                <Chip>{f.extraction_method}</Chip>
                {f.page && <Chip>p.{f.page}</Chip>}
              </div>
              <div>{f.statement}</div>
              {!f.validated && <div className="small errorbox">{(f.validation.reasons ?? []).join("; ")}</div>}
              {f.validation.retention_rejected && <div className="small muted">Not retained in memory: {f.validation.retention_rejected.join("; ")}</div>}
            </div>
          ))}
        </div>
      )}
      {tab === "edit" && s.kind === "note" && <NoteEditor ws={ws} source={s} onSaved={() => { reload(); onChanged(); }} />}
    </div>
  );
}

export function Sources() {
  const { ws, refreshWorkspace } = useOutletContext<Ctx>();
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const { data, error, loading, reload } = useAsync(() => api.sources(ws, q, status), [ws, q, status], (d) => (d?.some((s) => s.status === "queued" || s.status === "processing") ? 2000 : 20000));
  // keep the workspace header counts (sources / passages / findings / jobs) in step with the list as sources change state
  const signature = data?.map((s) => `${s.id}:${s.status}:${s.version}`).join(",");
  useEffect(() => {
    if (signature !== undefined) refreshWorkspace();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
  const open = params.get("open");
  const setOpen = (id: string | null) => setParams(id ? { open: id } : {}, { replace: true });

  return (
    <div className="split">
      <AddPanel ws={ws} onAdded={reload} />
      <Card title="Source library" actions={<button className="btn small" onClick={reload}>Refresh</button>}>
        <div className="row gap wrap">
          <input aria-label="Search sources" placeholder="Search titles…" value={q} onChange={(e) => setQ(e.target.value)} />
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all statuses</option>
            {["completed", "processing", "queued", "partial", "failed"].map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        {loading && !data && <Loading />}
        <ErrorBox error={error} onRetry={reload} />
        {data?.length === 0 && <Empty title={q || status ? "No sources match" : "No sources yet"}>Add a PDF, a URL, or a note to start building this workspace’s knowledge.</Empty>}
        <ul className="list">
          {data?.map((s) => (
            <li key={s.id} className="listitem" onClick={() => setOpen(s.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(s.id)}>
              <div className="row gap spread">
                <div className="grow">
                  <span className="kind">{KIND_ICON[s.kind]}</span> <strong>{s.title || "(untitled)"}</strong>
                  {s.error && <div className="small bad-text">{s.error.slice(0, 160)}</div>}
                  <div className="small muted">v{s.version} · {relTime(s.updated_at)}{s.duplicate_of ? " · duplicate" : ""}{s.status === "processing" ? ` · ${s.last_stage ?? ""}` : ""}</div>
                </div>
                <StatusChip status={s.status} />
              </div>
            </li>
          ))}
        </ul>
      </Card>
      <Drawer open={!!open} onClose={() => setOpen(null)} title="Source details" wide>
        {open && <SourceDrawerBody ws={ws} id={open} onChanged={reload} />}
      </Drawer>
    </div>
  );
}
