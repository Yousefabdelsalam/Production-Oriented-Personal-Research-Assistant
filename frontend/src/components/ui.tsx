import { ReactNode, useEffect } from "react";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden /> {label}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error?: Error | string; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="errorbox" role="alert">
      <strong>Something went wrong.</strong> {typeof error === "string" ? error : error.message}
      {onRetry && (
        <button className="btn small" onClick={onRetry} style={{ marginLeft: 8 }}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}

export function Chip({ children, tone = "neutral", title }: { children: ReactNode; tone?: string; title?: string }) {
  return (
    <span className={`chip tone-${tone}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, string> = {
  completed: "good", succeeded: "good", supported: "good", established: "info", new: "accent",
  processing: "info", running: "info", queued: "neutral", partial: "warn", hypothesis: "warn", contested: "bad", conflicting: "bad",
  failed: "bad", interrupted: "warn", insufficient: "neutral", rejected: "neutral", skipped: "neutral",
};
export function StatusChip({ status, label }: { status: string; label?: string }) {
  return <Chip tone={STATUS_TONE[status] ?? "neutral"}>{label ?? status}</Chip>;
}

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div className="row gap">{actions}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; badge?: ReactNode }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} className={value === t.id ? "tab active" : "tab"} onClick={() => onChange(t.id)}>
          {t.label} {t.badge}
        </button>
      ))}
    </div>
  );
}

export function Drawer({ open, title, onClose, children, wide = false }: { open: boolean; title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className={wide ? "drawer wide" : "drawer"} role="dialog" aria-modal aria-label={typeof title === "string" ? title : "Details"} onClick={(e) => e.stopPropagation()}>
        <header className="drawer-head">
          <h2>{title}</h2>
          <button className="btn small" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warn" | "bad" | "good"; children: ReactNode }) {
  return <div className={`notice tone-${tone}`}>{children}</div>;
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
