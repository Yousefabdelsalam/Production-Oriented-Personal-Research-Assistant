export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (isNaN(s)) return "—";
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} m ${Math.round((ms % 60000) / 1000)} s`;
}

export const MODE_LABEL: Record<string, string> = {
  supported: "Supported by evidence",
  partial: "Partially supported",
  conflicting: "Conflicting evidence",
  insufficient: "Insufficient evidence",
};

export const KIND_LABEL: Record<string, string> = {
  source_stated: "source states",
  user_note: "your note",
  inference: "inference",
  uncertain: "uncertain",
};

export const SECTION_TITLES: Record<string, string> = {
  core_concepts: "Core concepts and definitions",
  major_findings: "Major findings",
  relationships: "Relationships between concepts",
  methods: "Important methods and approaches",
  agreements: "Areas of agreement across sources",
  contradictions: "Contradictions and competing explanations",
  limitations: "Limitations and open research questions",
};

export function citationLabel(c: { source_title: string; page: number | null; section?: string | null }): string {
  const t = c.source_title.length > 38 ? c.source_title.slice(0, 36) + "…" : c.source_title;
  return c.page ? `${t}, p.${c.page}` : c.section ? `${t}, ${c.section}` : t;
}

/** Split text around the first verbatim (whitespace-insensitive) occurrence of `quote` so it can be highlighted. */
export function splitAroundQuote(text: string, quote: string): [string, string, string] | null {
  if (!quote) return null;
  const idx = text.indexOf(quote);
  if (idx >= 0) return [text.slice(0, idx), quote, text.slice(idx + quote.length)];
  const norm = (s: string) => s.replace(/\s+/g, " ");
  const nt = norm(text);
  const nq = norm(quote).trim();
  const i2 = nt.indexOf(nq);
  return i2 >= 0 ? [nt.slice(0, i2), nq, nt.slice(i2 + nq.length)] : null;
}
