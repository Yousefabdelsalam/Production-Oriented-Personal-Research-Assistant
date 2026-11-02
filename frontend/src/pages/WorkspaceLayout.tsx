import { NavLink, Outlet, useParams } from "react-router-dom";
import { api } from "../api";
import { PassageViewerProvider } from "../components/Evidence";
import { Chip, ErrorBox, Loading } from "../components/ui";
import { useAsync } from "../hooks";

export function WorkspaceLayout() {
  const { ws = "" } = useParams();
  const { data, error, loading, reload } = useAsync(() => api.workspace(ws), [ws], (d) => (d && d.jobs_active > 0 ? 3000 : 15000));
  const tabs = [
    ["sources", "Sources"],
    ["chat", "Research chat"],
    ["summary", "Living summary"],
    ["knowledge", "Knowledge"],
    ["inspector", "Harness inspector"],
  ];
  return (
    <PassageViewerProvider ws={ws}>
      <div className="stack">
        <div className="ws-head">
          <div>
            <h1>{data?.name ?? "Workspace"}</h1>
            {data && (
              <div className="row gap wrap small">
                <Chip>{data.sources} sources</Chip>
                <Chip>{data.passages} passages</Chip>
                <Chip>{data.findings} findings</Chip>
                <Chip>summary v{data.summary_version || "—"}</Chip>
                <Chip title="Hindsight memory bank for this workspace">bank <code>{data.bank_id}</code></Chip>
                {data.jobs_active > 0 && <Chip tone="info">{data.jobs_active} job(s) running</Chip>}
              </div>
            )}
          </div>
        </div>
        <nav className="subnav" aria-label="Workspace sections">
          {tabs.map(([to, label]) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "tab active" : "tab")}>
              {label}
            </NavLink>
          ))}
        </nav>
        {loading && !data && <Loading />}
        <ErrorBox error={error} />
        {data && <Outlet context={{ ws, workspace: data, refreshWorkspace: reload }} />}
      </div>
    </PassageViewerProvider>
  );
}
