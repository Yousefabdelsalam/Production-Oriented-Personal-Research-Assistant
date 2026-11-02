import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, Chip, Empty, ErrorBox, Loading, Notice, StatusChip } from "../components/ui";
import { relTime } from "../format";
import { useAction, useAsync } from "../hooks";

export function HealthBanner() {
  const { data, error } = useAsync(api.health, [], () => 15000);
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  return (
    <div className="stack">
      <div className="row gap wrap">
        <Chip tone={data.database.ok ? "good" : "bad"}>database {data.database.ok ? "ok" : "down"}</Chip>
        <Chip tone={data.hindsight.ok ? "good" : "bad"} title={data.hindsight.detail ?? data.hindsight.base_url}>
          Hindsight {data.hindsight.ok ? `v${data.hindsight.version}` : "unreachable"}
        </Chip>
        <Chip tone={data.generation_mode === "llm" ? "accent" : "neutral"} title={data.llm_model ?? "no LLM configured"}>
          generation: {data.generation_mode === "llm" ? `LLM (${data.llm_model})` : "extractive (no LLM)"}
        </Chip>
        <Chip>embeddings: {data.embedding}</Chip>
        <Chip tone={data.worker.enabled ? "good" : "warn"}>background worker {data.worker.enabled ? "on" : "off"}</Chip>
      </div>
      {data.notes.map((n, i) => (
        <Notice key={i} tone={data.hindsight.ok ? "info" : "warn"}>{n}</Notice>
      ))}
    </div>
  );
}

export function Dashboard() {
  const nav = useNavigate();
  const { data, error, loading, reload } = useAsync(api.workspaces, [], (d) => (d?.some((w) => w.jobs_active > 0) ? 3000 : 20000));
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const create = useAction(api.createWorkspace);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const w = await create.run(name.trim(), desc.trim());
    if (w) {
      setName("");
      setDesc("");
      nav(`/w/${w.id}/sources`);
    }
  }

  return (
    <div className="stack">
      <h1>Research workspaces</h1>
      <HealthBanner />
      <Card title="New workspace">
        <form className="row gap wrap" onSubmit={submit}>
          <input aria-label="Workspace name" placeholder="e.g. Agentic AI, LLMOps, AI memory systems" value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
          <input aria-label="Description" placeholder="Optional description" value={desc} onChange={(e) => setDesc(e.target.value)} />
          <button className="btn primary" disabled={create.busy || !name.trim()}>{create.busy ? "Creating…" : "Create workspace"}</button>
        </form>
        <ErrorBox error={create.error} />
        <p className="muted small">Each workspace gets its own Hindsight memory bank, so topics never leak into one another.</p>
      </Card>

      {loading && !data && <Loading />}
      <ErrorBox error={error} onRetry={reload} />
      {data && data.length === 0 && <Empty title="No workspaces yet">Create one above, then add PDFs, URLs or notes.</Empty>}
      <div className="grid">
        {data?.map((w) => (
          <Card key={w.id} title={<Link to={`/w/${w.id}/sources`}>{w.name}</Link>} actions={<Chip>summary v{w.summary_version || "—"}</Chip>}>
            <p className="muted">{w.description || "—"}</p>
            <div className="row gap wrap">
              <Chip>{w.sources} source{w.sources === 1 ? "" : "s"}</Chip>
              <Chip>{w.findings} findings</Chip>
              <Chip>{w.relationships} relationships</Chip>
              {Object.entries(w.sources_by_status).filter(([k]) => k !== "completed").map(([k, v]) => (
                <span key={k}><StatusChip status={k} label={`${v} ${k}`} /></span>
              ))}
            </div>
            <div className="row gap wrap small" style={{ marginTop: 8 }}>
              {w.jobs_active > 0 && <Link to={`/w/${w.id}/inspector`}><Chip tone="info">{w.jobs_active} job(s) running</Chip></Link>}
              {w.jobs_failed > 0 && <Link to={`/w/${w.id}/inspector?status=failed`}><Chip tone="bad">{w.jobs_failed} failed/interrupted</Chip></Link>}
              <span className="muted">updated {relTime(w.updated_at)}</span>
            </div>
            <div className="row gap small" style={{ marginTop: 8 }}>
              <Link className="btn small" to={`/w/${w.id}/chat`}>Ask</Link>
              <Link className="btn small" to={`/w/${w.id}/summary`}>Summary</Link>
              <Link className="btn small" to={`/w/${w.id}/knowledge`}>Knowledge</Link>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
