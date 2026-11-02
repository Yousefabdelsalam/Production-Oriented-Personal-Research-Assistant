"""Living research summary: content model, deterministic builder, validator, diff, markdown.

Design:
  * Items are stable (key = hash of the supporting findings / relationship id) so versions can be diffed and
    existing knowledge can be *preserved* across updates (carry-forward + a validator gate that rejects silent drops).
  * Item status is DERIVED, never asserted by a model: contested / hypothesis come from relationship status,
    'new' means first appeared in this version, 'established' means it was already in an earlier version.
  * Only the optional overview paragraph may be LLM-written, and it is validated (no figures that are not in the items).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.harness.injection import scan_text
from app.models import EvidencePassage, ExtractedFinding, ResearchRelationship, Source, Workspace
from app.services.textutil import numbers_in, truncate
from app.services.validators import SUPPORT_THRESHOLD, quote_in_text, support_score

SECTIONS: list[tuple[str, str]] = [
    ("core_concepts", "Core concepts and definitions"),
    ("major_findings", "Major findings"),
    ("relationships", "Relationships between concepts"),
    ("methods", "Important methods and approaches"),
    ("agreements", "Areas of agreement across sources"),
    ("contradictions", "Contradictions and competing explanations"),
    ("limitations", "Limitations and open research questions"),
]
SECTION_TITLES = dict(SECTIONS)
MAX_NEW_PER_SECTION = 20
KIND_SECTION = {"definition": "core_concepts", "finding": "major_findings", "result": "major_findings", "method": "methods",
                "limitation": "limitations", "open_question": "limitations"}
KIND_PRIORITY = {"definition": 0, "result": 1, "finding": 2, "method": 3, "limitation": 4, "open_question": 5}
REL_VERB = {"supports": "agree", "extends": "extend one another", "uses": "build on one another (uses)", "depends_on": "depend on one another",
            "applies_to": "apply to one another"}


class SummaryCitation(BaseModel):
    passage_id: str
    source_id: str
    source_title: str = ""
    page: int | None = None
    section: str | None = None
    quote: str


class SummaryItem(BaseModel):
    key: str
    section: str
    text: str
    kind: str = "finding"
    origin: Literal["source_stated", "user_note", "inference", "uncertain"] = "source_stated"
    status_hint: Literal["contested", "hypothesis"] | None = None
    finding_ids: list[str] = Field(default_factory=list)
    relationship_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    citations: list[SummaryCitation] = Field(default_factory=list)
    first_version: int = 0
    last_changed_version: int = 0


class SummaryContent(BaseModel):
    overview: str
    overview_mode: str = "deterministic"
    sections: dict[str, list[SummaryItem]] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    def all_items(self) -> list[SummaryItem]:
        return [i for items in self.sections.values() for i in items]


def item_status(item: SummaryItem, version: int) -> str:
    if item.status_hint:
        return item.status_hint
    return "new" if item.first_version == version else "established"


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _cite(f: ExtractedFinding, titles: dict[str, str]) -> SummaryCitation:
    return SummaryCitation(passage_id=f.passage_id, source_id=f.source_id, source_title=titles.get(f.source_id, ""), page=f.page,
                           section=f.section, quote=f.quote)


# ----------------------------------------------------------------------------------------------------------------------
def build_items(db: Session, ws_id: str) -> tuple[list[SummaryItem], dict[str, Any]]:
    """Pure projection of the workspace's validated knowledge into summary items (deterministic)."""
    titles = {s.id: s.title for s in db.query(Source).filter_by(workspace_id=ws_id).all()}
    findings = (db.query(ExtractedFinding)
                .filter_by(workspace_id=ws_id, validated=True, superseded=False).order_by(ExtractedFinding.created_at).all())
    by_id = {f.id: f for f in findings}
    items: list[SummaryItem] = []
    for f in sorted(findings, key=lambda f: (KIND_PRIORITY.get(f.kind, 9), -f.confidence)):
        sec = KIND_SECTION.get(f.kind)
        if not sec:
            continue
        prefix = TEMPLATE_PREFIXES[0] if f.epistemic == "user_note" else ""
        items.append(SummaryItem(key=_key(sec, f.id), section=sec, text=prefix + f.statement, kind=f.kind,
                                 origin="user_note" if f.epistemic == "user_note" else f.epistemic if f.epistemic in ("inference", "uncertain") else "source_stated",
                                 finding_ids=[f.id], source_ids=[f.source_id], citations=[_cite(f, titles)]))
    rels = db.query(ResearchRelationship).filter_by(workspace_id=ws_id, active=True).filter(ResearchRelationship.status != "rejected").all()
    for r in rels:
        a, b = by_id.get(r.finding_a_id), by_id.get(r.finding_b_id)
        if a is None or b is None:
            continue
        ta, tb = titles.get(a.source_id, "?"), titles.get(b.source_id, "?")
        pair = f"“{truncate(a.quote, 200)}” ({ta}) and “{truncate(b.quote, 200)}” ({tb})"
        if r.predicate == "contradicts":
            sec, text = "contradictions", f"Possible contradiction: {pair}."
            hint = "contested" if r.status == "supported" else "hypothesis"
        elif r.predicate == "supports":
            sec, text = "agreements", f"Sources agree: {pair}."
            hint = None if r.status == "supported" else "hypothesis"
        else:
            sec, text = "relationships", f"These sources {REL_VERB.get(r.predicate, r.predicate)}: {pair}."
            hint = None if r.status == "supported" else "hypothesis"
        items.append(SummaryItem(key=_key(sec, r.id), section=sec, text=text, kind=r.predicate, origin="inference", status_hint=hint,
                                 finding_ids=[a.id, b.id], relationship_id=r.id, source_ids=sorted({a.source_id, b.source_id}),
                                 citations=[_cite(a, titles), _cite(b, titles)]))
    stats = {"findings": len(findings), "sources": len({f.source_id for f in findings}), "relationships": len(rels),
             "unverified_relationships": sum(1 for r in rels if r.status != "supported"),
             "concept_counts": Counter(c for f in findings for c in (f.concepts or [])).most_common(6)}
    return items, stats


def assemble(items: list[SummaryItem], stats: dict[str, Any], prev: SummaryContent | None, valid_prev_keys: set[str], ws_name: str,
             overview: str | None = None, overview_mode: str = "deterministic") -> SummaryContent:
    """Carry forward every still-valid previous item (preservation), then add new items up to the per-section cap."""
    new_by_key = {i.key: i for i in items}
    sections: dict[str, list[SummaryItem]] = {k: [] for k, _ in SECTIONS}
    used: set[str] = set()
    if prev is not None:
        for sec, old_items in prev.sections.items():
            for old in old_items:
                if old.key in valid_prev_keys and old.key in new_by_key:
                    fresh = new_by_key[old.key]
                    # refresh text/status/citations from current knowledge, keep history fields
                    sections.setdefault(sec, []).append(fresh.model_copy(update={"first_version": old.first_version, "last_changed_version": old.last_changed_version}))
                    used.add(old.key)
    added: Counter = Counter()
    deferred = 0
    for it in items:
        if it.key in used:
            continue
        if added[it.section] >= MAX_NEW_PER_SECTION:
            deferred += 1  # not lost: it is picked up by a later update (and reported, so the user knows the summary is not yet complete)
            continue
        added[it.section] += 1
        sections.setdefault(it.section, []).append(it)
    srcs: list[str] = []
    for it in (i for v in sections.values() for i in v):
        for s in it.source_ids:
            if s not in srcs:
                srcs.append(s)
    n_open = sum(1 for i in sections.get("limitations", []))
    n_contra = len(sections.get("contradictions", []))
    n_unver = sum(1 for i in sections.get("contradictions", []) if i.status_hint == "hypothesis")
    top = ", ".join(c for c, _ in stats.get("concept_counts", [])[:5]) or "n/a"
    det = (f"Workspace “{ws_name}” currently draws on {len(srcs)} source(s) with {stats.get('findings', 0)} validated findings. "
           f"Most frequent concepts: {top}. {n_contra} possible contradiction(s) are recorded ({n_unver} unverified) and "
           f"{n_open} limitation/open-question item(s) are tracked.")
    return SummaryContent(overview=overview or det, overview_mode=overview_mode if overview else "deterministic",
                          sections=sections, source_ids=srcs,
                          stats={k: v for k, v in stats.items() if k != "concept_counts"} | {"top_concepts": top, "deferred_items": deferred})


# ----------------------------------------------------------------------------------------------------------------------
class ValidationReport(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)
    checked_items: int = 0
    checked_citations: int = 0


def removal_reason(db: Session, ws_id: str, old: SummaryItem) -> str | None:
    """Why a previously summarised item may legitimately disappear (None => it must be preserved)."""
    if old.relationship_id:
        r = db.get(ResearchRelationship, old.relationship_id)
        if r is None or r.workspace_id != ws_id:
            return "relationship no longer exists"
        if r.status == "rejected":
            return "relationship was rejected by the user"
        if not r.active:
            return "relationship no longer active (a supporting finding was superseded)"
    for fid in old.finding_ids:
        f = db.get(ExtractedFinding, fid)
        if f is None or f.workspace_id != ws_id:
            return "supporting finding no longer exists"
        if f.superseded:
            return "supporting finding was superseded by a newer source version"
        if not f.validated:
            return "supporting finding is no longer validated"
    return None


TEMPLATE_PREFIXES = ("(your note) ", "Possible contradiction: ")


def strip_template(text: str) -> str:
    """Remove the fixed labels the summary builder prepends. They are not part of the claim, so they must not count against it:
    a finding validated at ingestion (statement vs. its quote) must not fail here only because of a label."""
    for p in TEMPLATE_PREFIXES:
        if text.startswith(p):
            return text[len(p):]
    return text


def validate_content(db: Session, ws_id: str, content: SummaryContent, prev: SummaryContent | None, *,
                     require_current: bool = True) -> ValidationReport:
    problems: list[str] = []
    n_cites = 0
    ws_sources = {s.id for s in db.query(Source).filter_by(workspace_id=ws_id).all()}
    seen_keys: set[str] = set()
    all_items = content.all_items()
    for it in all_items:
        if it.section not in SECTION_TITLES:
            problems.append(f"item {it.key}: unknown section '{it.section}'")
        if it.key in seen_keys:
            problems.append(f"item {it.key}: duplicate key")
        seen_keys.add(it.key)
        if not it.finding_ids or not it.citations:
            problems.append(f"item {it.key}: no supporting finding/citation")
        if scan_text(it.text):
            problems.append(f"item {it.key}: instruction-like text")
        for fid in it.finding_ids:
            f = db.get(ExtractedFinding, fid)
            if f is None or f.workspace_id != ws_id:
                problems.append(f"item {it.key}: finding {fid} not found in this workspace")
            elif require_current and (f.superseded or not f.validated):
                problems.append(f"item {it.key}: finding {fid} is superseded or not validated")
        quotes: list[str] = []
        for c in it.citations:
            n_cites += 1
            p = db.get(EvidencePassage, c.passage_id)
            if p is None or p.workspace_id != ws_id:
                problems.append(f"item {it.key}: citation passage {c.passage_id} does not resolve")
                continue
            if c.source_id not in ws_sources or p.source_id != c.source_id:
                problems.append(f"item {it.key}: citation source mismatch for {c.passage_id}")
            if not quote_in_text(c.quote, p.text):
                problems.append(f"item {it.key}: quote is not a verbatim excerpt of passage {c.passage_id}")
            quotes.append(c.quote + " " + c.source_title)
        if quotes:
            # strip fixed template prose, then require the claim to be supported by its own quotes
            sup = support_score(strip_template(it.text), quotes)
            if sup.score < SUPPORT_THRESHOLD or not sup.numbers_ok:
                problems.append(f"item {it.key}: text not supported by its citations (score {sup.score}, numbers {sup.missing_numbers})")
    if scan_text(content.overview) or len(content.overview) > 1500:
        problems.append("overview: instruction-like text or too long")
    ws_row = db.get(Workspace, ws_id)
    allowed_nums = set().union(*[numbers_in(i.text) for i in all_items], numbers_in(str(content.stats)),
                               numbers_in(ws_row.name if ws_row else ""),  # the overview quotes the workspace name; its digits are not invented figures
                               {str(len(content.source_ids)), str(len(content.sections.get("contradictions", []))), str(len(content.sections.get("limitations", [])))}) if all_items else set()
    bad_nums = sorted(n for n in numbers_in(content.overview) if n not in allowed_nums and n not in {str(i) for i in range(0, 200)})
    if bad_nums:
        problems.append(f"overview: figures not found in the summary items: {bad_nums}")
    if set(content.source_ids) - ws_sources:
        problems.append("source list contains sources outside this workspace")
    if prev is not None:  # preservation gate: nothing disappears without a recorded reason
        for old in prev.all_items():
            if old.key not in seen_keys and removal_reason(db, ws_id, old) is None:
                problems.append(f"existing item {old.key} was dropped without justification: {truncate(old.text, 80)}")
    return ValidationReport(ok=not problems, problems=problems, checked_items=len(all_items), checked_citations=n_cites)


# ----------------------------------------------------------------------------------------------------------------------
def diff_contents(old: SummaryContent | None, new: SummaryContent, removal_reasons: dict[str, str] | None = None) -> dict[str, Any]:
    old_items = {i.key: i for i in old.all_items()} if old else {}
    new_items = {i.key: i for i in new.all_items()}
    added = [i for k, i in new_items.items() if k not in old_items]
    removed = [{"key": k, "section": i.section, "text": i.text, "reason": (removal_reasons or {}).get(k, "")} for k, i in old_items.items() if k not in new_items]
    changed = []
    for k, n in new_items.items():
        o = old_items.get(k)
        if o is None:
            continue
        fields = [f for f in ("text", "status_hint", "section") if getattr(o, f) != getattr(n, f)]
        if {c.passage_id for c in o.citations} != {c.passage_id for c in n.citations}:
            fields.append("citations")
        if fields:
            changed.append({"key": k, "section": n.section, "fields": fields, "before": o.text, "after": n.text})
    return {
        "added": [{"key": i.key, "section": i.section, "text": i.text, "sources": i.source_ids} for i in added],
        "removed": removed, "changed": changed, "unchanged": len(new_items) - len(added) - len(changed),
        "overview_changed": bool(old and old.overview != new.overview),
        "meaningful": bool(added or removed or changed),
    }


def render_markdown(content: SummaryContent, ws_name: str, version: int, titles: dict[str, str], kinds: dict[str, str]) -> str:
    label = {s: f"S{n}" for n, s in enumerate(content.source_ids, start=1)}
    lines = [f"# {ws_name} — living research summary (v{version})", "", f"> {content.overview}", ""]
    new_items = [i for i in content.all_items() if i.first_version == version and version > 1]
    for sec, title in SECTIONS:
        items = content.sections.get(sec, [])
        lines.append(f"## {title}")
        if not items:
            lines += ["_Nothing recorded yet._", ""]
            continue
        for it in items:
            st = item_status(it, version).upper()
            cites = ", ".join(sorted({f"{label.get(c.source_id, '?')}" + (f" p.{c.page}" if c.page else "") for c in it.citations}))
            lines.append(f"- **[{st}]** {it.text} _[{cites}]_" + ("" if it.origin == "source_stated" else f" _(origin: {it.origin.replace('_', ' ')})_"))
        lines.append("")
    if version > 1:
        lines += ["## New developments in this version"]
        lines += [f"- {truncate(i.text, 160)}" for i in new_items] or ["_No new items in this version._"]
        lines.append("")
    lines += ["## Supporting sources"]
    lines += [f"{n}. **S{n}** {titles.get(s, s)} ({kinds.get(s, '?')}) — `{s}`" for n, s in enumerate(content.source_ids, start=1)] or ["_None yet._"]
    lines += ["", "_Status legend: NEW = first appears in this version · ESTABLISHED = present in an earlier version · CONTESTED = sources disagree · "
              "HYPOTHESIS = unverified automatic link. Items marked 'inference' are not quotations from any source._"]
    return "\n".join(lines)


def parse_content(raw: dict[str, Any]) -> SummaryContent:
    try:
        return SummaryContent.model_validate(raw)
    except ValidationError as e:  # corrupted stored version must never be silently accepted
        raise ValueError(f"stored summary content is invalid: {e.errors()[:2]}") from e
