import { FormEvent, useState } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";
import { getToken, setToken } from "./api";
import { Dashboard } from "./pages/Dashboard";
import { Evaluation } from "./pages/Evaluation";
import { Chat } from "./pages/Chat";
import { Inspector } from "./pages/Inspector";
import { Knowledge } from "./pages/Knowledge";
import { Sources } from "./pages/Sources";
import { Summary } from "./pages/Summary";
import { WorkspaceLayout } from "./pages/WorkspaceLayout";

/** Only needed when the backend sets API_TOKEN; stored in this browser only. */
function TokenField() {
  const [value, setValue] = useState(getToken());
  const [saved, setSaved] = useState(false);
  function submit(e: FormEvent) {
    e.preventDefault();
    setToken(value.trim());
    setSaved(true);
    window.setTimeout(() => window.location.reload(), 300);
  }
  return (
    <form className="row gap small" onSubmit={submit}>
      <input type="password" aria-label="API token" placeholder="API token (if required)" value={value} onChange={(e) => setValue(e.target.value)} />
      <button className="btn small">{saved ? "Saved" : "Set"}</button>
    </form>
  );
}

export function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          Research Assistant
        </Link>
        <nav className="row gap">
          <Link to="/">Workspaces</Link>
          <Link to="/evaluation">Evaluation</Link>
        </nav>
        <div className="grow" />
        <TokenField />
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/evaluation" element={<Evaluation />} />
          <Route path="/w/:ws" element={<WorkspaceLayout />}>
            <Route index element={<Navigate to="sources" replace />} />
            <Route path="sources" element={<Sources />} />
            <Route path="chat" element={<Chat />} />
            <Route path="summary" element={<Summary />} />
            <Route path="knowledge" element={<Knowledge />} />
            <Route path="inspector" element={<Inspector />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
