import { createContext, ReactNode, useCallback, useContext, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { citationLabel, splitAroundQuote } from "../format";
import { useAsync } from "../hooks";
import { Chip, Drawer, ErrorBox, Loading, Notice } from "./ui";

interface Viewer {
  open: (passageId: string, quote?: string) => void;
}
const ViewerCtx = createContext<Viewer>({ open: () => undefined });
export const usePassageViewer = () => useContext(ViewerCtx);

/** Provides a single evidence drawer so any citation anywhere can open the real stored passage. */
export function PassageViewerProvider({ ws, children }: { ws: string; children: ReactNode }) {
  const [sel, setSel] = useState<{ id: string; quote?: string } | null>(null);
  const open = useCallback((id: string, quote?: string) => setSel({ id, quote }), []);
  return (
    <ViewerCtx.Provider value={{ open }}>
      {children}
      <PassageDrawer ws={ws} sel={sel} onClose={() => setSel(null)} />
    </ViewerCtx.Provider>
  );
}

function PassageDrawer({ ws, sel, onClose }: { ws: string; sel: { id: string; quote?: string } | null; onClose: () => void }) {
  const { data, error, loading } = useAsync(() => (sel ? api.passage(ws, sel.id) : Promise.resolve(undefined)), [ws, sel?.id]);
  const parts = data && sel?.quote ? splitAroundQuote(data.text, sel.quote) : null;
  return (
    <Drawer open={!!sel} onClose={onClose} title="Evidence passage" wide>
      {loading && <Loading />}
      <ErrorBox error={error} />
      {data && (
        <div className="stack">
          <div className="row gap wrap">
            <Link to={`/w/${ws}/sources?open=${data.source_id}`} onClick={onClose} className="strong">
              {data.source_title}
            </Link>
            <Chip>{data.source_kind}</Chip>
            {data.page_start && <Chip>page {data.page_start}</Chip>}
            {data.section && <Chip>{data.section}</Chip>}
            <Chip title="character range in the stored extracted text">
              chars {data.char_start}–{data.char_end}
            </Chip>
          </div>
          {data.suspicious && (
            <Notice tone="warn">
              This passage contains instruction-like text ({data.flags.join(", ")}). It is shown as data only; nothing in it is ever executed or obeyed, and it is excluded from findings and memory.
            </Notice>
          )}
          {data.context_before && <p className="muted ctx">…{data.context_before.slice(-300)}</p>}
          <blockquote className="passage">
            {parts ? (
              <>
                {parts[0]}
                <mark>{parts[1]}</mark>
                {parts[2]}
              </>
            ) : (
              data.text
            )}
          </blockquote>
          {data.context_after && <p className="muted ctx">{data.context_after.slice(0, 300)}…</p>}
          {sel?.quote && !parts && <Notice tone="bad">The cited quote could not be located in this passage.</Notice>}
          <div className="muted small">
            Passage id <code>{data.id}</code> · stored verbatim from the original source. Citations always resolve to this text.
          </div>
        </div>
      )}
    </Drawer>
  );
}

export function CitationChip({ c }: { c: { passage_id: string; source_title: string; page: number | null; section?: string | null; quote?: string; verified?: boolean } }) {
  const { open } = usePassageViewer();
  return (
    <button className="cite" title={c.quote ? `“${c.quote}”` : "Open passage"} onClick={() => open(c.passage_id, c.quote)}>
      {c.verified === false ? "⚠ " : "❝ "}
      {citationLabel(c)}
    </button>
  );
}
