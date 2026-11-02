"""Question-answering building blocks: intent/plan, extractive + LLM generation, deterministic claim validation.

The harness (workflows/qa.py) sequences these; nothing here touches Hindsight or writes to the DB.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.harness.injection import scan_text
from app.schemas import AnswerMode, Citation, Claim, ConflictNote
from app.services.llm import LLMClient, Usage, with_rules, wrap_untrusted
from app.services.retrieval import EvidenceHit
from app.services.textutil import norm_for_match, numbers_in, sentences, stem, tokens, truncate
from app.services.validators import SUPPORT_THRESHOLD, quote_in_text, support_score

# ---------------------------------------------------------------------------------------------
# Intent classification + retrieval plan (deterministic: the harness, not a model, decides what to fetch)
# ---------------------------------------------------------------------------------------------
_RX_MEMORY = re.compile(r"\b(what (did|have) i (say|write|wrote|note|noted|think|decide|decided)|my (own )?notes?|i noted|i wrote|earlier|previously|last time|remind me|what do i (think|believe))\b", re.I)
_RX_COMPARE = re.compile(r"\b(compare|comparison|versus|vs\.?|differences?|differ|relat(e|es|ed|ionship)|connect(ion|ed)?|agree|disagree|contradict\w*|conflict\w*|across|both|similar(ity|ities)?|combine|together)\b", re.I)
_RX_DEFINE = re.compile(r"^\s*(what is|what are|what's|define|definition of|what does .+ mean|meaning of)\b", re.I)
_RX_SUMMARY = re.compile(r"\b(summar\w+|overview|state of|what do we know|main (findings|results)|key (findings|takeaways)|big picture)\b", re.I)
_RX_FOLLOWUP = re.compile(r"\b(it|its|that|those|they|them|this|these|the (paper|source|article|note|second|first|latter|former))\b", re.I)

Intent = Literal["memory_recall", "source_specific", "comparison", "definition", "summary", "factual"]


@dataclass
class QAPlan:
    intent: Intent
    source_ids: list[str] = field(default_factory=list)
    focus_source_ids: list[str] = field(default_factory=list)  # boosted, not exclusive
    use_memory: bool = True
    memory_budget: str = "low"
    memory_scoped_to_sources: bool = False
    evidence_k: int = 6
    max_per_source: int | None = None
    expanded_query: str = ""
    rationale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _title_matches(question_toks: set[str], title: str, question: str = "") -> bool:
    # "Name: Subtitle" titles are commonly referred to by the short name only
    alias = title.split(":")[0].strip() if ":" in title else ""
    if alias and 4 <= len(alias) <= 40 and norm_for_match(alias) in norm_for_match(question):
        return True
    tt = [stem(t) for t in tokens(title) if len(t) > 2]
    if len(tt) < 1:
        return False
    qs = {stem(t) for t in question_toks}
    hit = sum(1 for t in tt if t in qs)
    return hit >= 2 and hit / len(tt) >= 0.6 if len(tt) >= 2 else hit == 1 and len(tt[0]) > 5


def classify_question(question: str, sources: list[tuple[str, str, str]], previous: list[dict] | None = None) -> QAPlan:
    """sources: [(id, title, kind)] of sources in the workspace; previous: [{question, source_ids}] recent turns."""
    qtoks = set(tokens(question))
    q = question.strip()
    matched = [sid for sid, title, _ in sources if _title_matches(qtoks, title, q)]
    expanded = q
    rationale: list[str] = []
    # follow-up resolution: short question with anaphora inherits the previous turn's topic and source focus
    if previous and (len(qtoks) <= 6 and _RX_FOLLOWUP.search(q)):
        prev = previous[-1]
        if not matched and prev.get("source_ids") and re.search(r"\b(this|that|the) (paper|source|article|note)\b|\b(it|its|their|they)\b", q, re.I):
            matched = list(prev["source_ids"])[:2]
            rationale.append("follow-up: pronoun resolved to the source(s) cited in the previous answer")
        else:  # no source to inherit: carry the previous question's wording instead
            expanded = f"{q} {prev.get('question', '')}".strip()
            rationale.append("follow-up: previous question's topic added to the retrieval query")

    if _RX_MEMORY.search(q):
        plan = QAPlan("memory_recall", use_memory=True, memory_budget="mid", evidence_k=4, expanded_query=expanded)
        note_ids = [sid for sid, _, kind in sources if kind == "note"]
        if note_ids:
            plan.source_ids = note_ids
            rationale.append("question asks about the user's own notes/history → restrict evidence to note sources")
        rationale.append("memory recall prioritised (mid budget)")
    elif matched and len(matched) <= 2 and not _RX_COMPARE.search(q):
        plan = QAPlan("source_specific", source_ids=matched, use_memory=True, memory_budget="low", memory_scoped_to_sources=True,
                      evidence_k=6, expanded_query=expanded)
        rationale.append(f"question names {len(matched)} specific source(s) → evidence and memory restricted to them")
    elif _RX_COMPARE.search(q) or len(matched) > 1:
        # restrict only when the user named 2+ sources; a single named source is just boosted (the other side of a
        # comparison is usually unnamed)
        plan = QAPlan("comparison", source_ids=matched if len(matched) > 1 else [], focus_source_ids=matched if len(matched) == 1 else [],
                      use_memory=True, memory_budget="mid", evidence_k=8, max_per_source=3, expanded_query=expanded)
        rationale.append("relationship/comparison question → evidence diversified across sources (≤3 per source) + mid-budget memory")
    elif _RX_DEFINE.search(q):
        plan = QAPlan("definition", use_memory=True, memory_budget="low", evidence_k=4, expanded_query=expanded)
        rationale.append("definition question → few, precise passages; low-budget memory")
    elif _RX_SUMMARY.search(q):
        plan = QAPlan("summary", use_memory=True, memory_budget="mid", evidence_k=8, max_per_source=2, expanded_query=expanded)
        rationale.append("overview question → broad evidence (≤2 per source) + mid-budget memory")
    else:
        plan = QAPlan("factual", use_memory=True, memory_budget="low", evidence_k=6, expanded_query=expanded)
        rationale.append("factual lookup → targeted evidence; low-budget memory")
    plan.rationale = rationale
    return plan


# ---------------------------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------------------------
@dataclass
class DraftClaim:
    text: str
    kind: str
    evidence: list[tuple[str, str]]  # (passage_id, quote)
    memory_refs: list[str] = field(default_factory=list)


_PREFER_KINDS: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(r"\b(limitations?|weakness\w*|drawbacks?|shortcomings?|downsides?|caveats?|fail\w*)\b", re.I), {"limitation", "open_question"}),
    (re.compile(r"^\s*(what is|what are|what's|define|definition)\b|\bmeaning of\b|\bdefined\b", re.I), {"definition"}),
    (re.compile(r"\b(how much|how many|how well|percent|accuracy|improv\w+|reduc\w+|latency|result|outperform\w*|performance)\b", re.I), {"result"}),
    (re.compile(r"\b(method|approach|technique|how (does|do|is)|propos\w+|architecture)\b", re.I), {"method"}),
    (re.compile(r"\b(open question|unknown|unresolved|future work)\b", re.I), {"open_question"}),
]


def preferred_kinds(question: str) -> set[str]:
    out: set[str] = set()
    for rx, kinds in _PREFER_KINDS:
        if rx.search(question):
            out |= kinds
    return out


def best_sentences(question_toks: set[str], hit: EvidenceHit, limit: int = 1, question: str = "") -> list[tuple[float, str]]:
    from app.services.findings import classify_sentence  # local import: findings imports models, avoid cycles

    qs = {stem(t) for t in question_toks}
    prefer = preferred_kinds(question)
    sec = {stem(t) for t in tokens(hit.section or "")}  # a heading like "3. Limitations" speaks for its sentences
    # query words that merely name the source ("…of MemoryBank-Lite") say little about which sentence answers the question
    title_stems = {stem(t) for t in tokens(hit.source_title)}
    weight = {s: (0.3 if s in title_stems else 1.0) for s in qs}
    denom = sum(weight.values()) or 1.0
    scored: list[tuple[float, str]] = []
    for s in sentences(hit.text):
        # headings / titles are not claims: require a full sentence
        if len(s) < 25 or not s.rstrip().endswith((".", "!", "?")) or len(s.split()) < 6:
            continue
        if scan_text(s):
            continue  # instruction-like text inside a document is data to be shown on request, never an answer (same rule as findings)
        ss = {stem(t) for t in tokens(s)}
        if not ss:
            continue
        matched_terms = qs & (ss | sec)
        if not matched_terms:
            continue
        overlap = sum(weight[s] for s in matched_terms)
        # prefer sentences that cover query terms but are not bloated
        score = overlap / denom + 0.1 * (1 if numbers_in(s) else 0) - 0.0005 * max(0, len(s) - 220)
        if prefer and classify_sentence(s)[0] in prefer:
            score += 0.4
        scored.append((score, s))
    scored.sort(key=lambda t: -t[0])
    return scored[:limit]


def generate_extractive(question: str, hits: list[EvidenceHit], plan: QAPlan, max_claims: int = 6) -> list[DraftClaim]:
    """Deterministic: each claim IS a verbatim sentence from a retrieved passage (no paraphrase, so nothing to hallucinate)."""
    qt = set(tokens(plan.expanded_query or question))
    cands: list[tuple[float, int, EvidenceHit, str]] = []
    for rank, hit in enumerate(hits):
        for score, sent in best_sentences(qt, hit, limit=1, question=question):
            if score >= 0.2:
                cands.append((score, rank, hit, sent))
    if not cands:
        return []
    best = max(c[0] for c in cands)
    cands = [c for c in cands if c[0] >= 0.5 * best]  # relative relevance cut: no tail noise
    cands.sort(key=lambda c: (-c[0], c[1]))
    drafts: list[DraftClaim] = []
    per_source: dict[str, int] = {}
    seen: set[str] = set()
    for score, _, hit, sent in cands:
        if plan.max_per_source and per_source.get(hit.source_id, 0) >= plan.max_per_source:
            continue
        key = norm_for_match(sent)
        if key in seen:
            continue
        seen.add(key)
        per_source[hit.source_id] = per_source.get(hit.source_id, 0) + 1
        drafts.append(DraftClaim(sent, "user_note" if hit.source_kind == "note" else "source_stated", [(hit.passage_id, sent)]))
        if len(drafts) >= max_claims:
            break
    return drafts


class _LLMClaim(BaseModel):
    text: str
    kind: str = "source_stated"
    evidence: list[str] = Field(default_factory=list)  # labels E1..
    quote: str = ""
    memory: list[str] = Field(default_factory=list)  # labels M1..


class _LLMAnswer(BaseModel):
    claims: list[_LLMClaim] = Field(default_factory=list)


ANSWER_SYSTEM = (
    "You answer a research question using ONLY the supplied evidence passages (E#) and, as secondary context, recalled "
    "memories (M#). Return claims. Each claim: `text` (one self-contained statement), `kind` "
    "('source_stated' if an evidence passage states it; 'user_note' if it comes from a user note; 'inference' if you are "
    "concluding or connecting ideas beyond what any single passage says), `evidence` (labels of the passages that support it), "
    "`quote` (a contiguous span copied VERBATIM from one cited passage; required for source_stated), and `memory` (labels, optional). "
    "Never state a fact that is not in the evidence. If the evidence does not answer the question, return no claims. "
    "Memories are NOT proof: a claim supported only by memory must be omitted."
)


def generate_llm(llm: LLMClient, question: str, hits: list[EvidenceHit], memories: list[tuple[str, str]], plan: QAPlan
                 ) -> tuple[list[DraftClaim], Usage]:
    labels = {f"E{i + 1}": h for i, h in enumerate(hits)}
    ev = "\n\n".join(
        wrap_untrusted(f"{lab} source=\"{h.source_title}\" page={h.page_start} section=\"{h.section or '-'}\"", h.text)
        for lab, h in labels.items()
    )
    mem_labels = {f"M{i + 1}": m for i, (mid, _) in enumerate(memories) for m in [mid]}
    mm = "\n".join(wrap_untrusted(f"M{i + 1}", t) for i, (_, t) in enumerate(memories)) or "(none)"
    parsed, usage = llm.complete_json(
        purpose="generate_answer", system=with_rules(ANSWER_SYSTEM),
        user=f"Question: {question}\nIntent: {plan.intent}\n\nEVIDENCE:\n{ev}\n\nMEMORIES:\n{mm}", schema=_LLMAnswer, max_tokens=1800,
    )
    out: list[DraftClaim] = []
    for c in parsed.claims:
        ev_hits = [labels[e.strip().upper()] for e in c.evidence if e.strip().upper() in labels]
        refs = [mem_labels[m.strip().upper()] for m in c.memory if m.strip().upper() in mem_labels]
        quote = c.quote.strip()
        evid: list[tuple[str, str]] = []
        for h in ev_hits:
            evid.append((h.passage_id, quote if quote and quote_in_text(quote, h.text) else ""))
        out.append(DraftClaim(c.text.strip(), c.kind if c.kind in ("source_stated", "user_note", "inference", "uncertain") else "uncertain", evid, refs))
    return out, usage


# ---------------------------------------------------------------------------------------------
# Deterministic validation (checker)
# ---------------------------------------------------------------------------------------------
@dataclass
class ValidationReport:
    claims: list[Claim]
    dropped: list[dict]
    checks: list[dict]


def validate_claims(drafts: list[DraftClaim], passages: dict[str, EvidenceHit]) -> ValidationReport:
    """passages: the retrieved evidence for this answer, keyed by passage id. A citation must resolve to one of them."""
    claims: list[Claim] = []
    dropped: list[dict] = []
    checks: list[dict] = []
    for i, d in enumerate(drafts, start=1):
        cid = f"c{i}"
        cites: list[Citation] = []
        problems: list[str] = []
        for pid, quote in d.evidence:
            hit = passages.get(pid)
            if hit is None:
                problems.append(f"citation {pid} does not resolve to retrieved evidence")
                continue
            verified = bool(quote) and quote_in_text(quote, hit.text)
            if quote and not verified:
                problems.append(f"quote for {pid} is not a verbatim excerpt")
                quote = ""
            cites.append(Citation(passage_id=pid, source_id=hit.source_id, source_title=hit.source_title, source_kind=hit.source_kind,
                                  page=hit.page_start, section=hit.section, quote=quote, verified=verified))
        if scan_text(d.text):
            problems.append("claim text is instruction-like (possible prompt injection) and is not presented as an answer")
        if d.kind == "source_stated" and not any(c.verified for c in cites):
            problems.append("source_stated claim lacks a verified verbatim quote")
        if d.kind in ("source_stated", "user_note", "inference") and not cites:
            problems.append("no resolvable citation")
        sup = support_score(d.text, [passages[c.passage_id].text for c in cites if c.passage_id in passages]) if cites else None
        if sup is not None and d.kind != "inference" and (sup.score < SUPPORT_THRESHOLD or not sup.numbers_ok):
            problems.append(f"claim is not adequately supported by its citations (score {sup.score}; unsupported numbers {sup.missing_numbers})")
        if sup is not None and d.kind == "inference" and sup.score < 0.3:
            problems.append("inference is not anchored in the cited evidence")
        checks.append({"claim": cid, "support": sup.score if sup else None, "problems": problems})
        if problems:
            dropped.append({"claim": truncate(d.text, 120), "problems": problems})
            continue
        claims.append(Claim(id=cid, text=d.text, kind=d.kind, citations=cites, memory_refs=d.memory_refs,
                            support=sup.score if sup else None, status="supported"))
    # re-number so ids are contiguous
    for n, c in enumerate(claims, start=1):
        c.id = f"c{n}"
    return ValidationReport(claims, dropped, checks)


class _CheckVerdict(BaseModel):
    verdicts: list[dict] = Field(default_factory=list)  # [{claim: "c1", verdict: "supported|partial|unsupported", reason: ""}]


CHECK_SYSTEM = (
    "You are a strict fact-checker. For each claim decide whether the QUOTED EVIDENCE alone supports it. "
    "Return verdicts: [{claim: id, verdict: 'supported'|'partial'|'unsupported', reason: short}]. "
    "Do not use outside knowledge. You are a second opinion only."
)


def llm_check_claims(llm: LLMClient, claims: list[Claim], passages: dict[str, EvidenceHit]) -> tuple[dict[str, str], Usage | None]:
    """Optional LLM second opinion. Callers may only DOWNGRADE claims using this result."""
    if not claims:
        return {}, None
    body = "\n\n".join(
        f"CLAIM {c.id}: {c.text}\n" + wrap_untrusted(f"evidence for {c.id}", " ... ".join(passages[x.passage_id].text for x in c.citations if x.passage_id in passages))
        for c in claims
    )
    parsed, usage = llm.complete_json(purpose="check_claims", system=with_rules(CHECK_SYSTEM), user=body, schema=_CheckVerdict, max_tokens=1200)
    return {v.get("claim", ""): str(v.get("verdict", "")).lower() for v in parsed.verdicts}, usage


# ---------------------------------------------------------------------------------------------
# Mode + answer text
# ---------------------------------------------------------------------------------------------
GENERIC = {"explain", "describe", "tell", "give", "show", "list", "long", "work", "works", "paper", "things", "thing", "mean", "means"}


def question_coverage(question: str, claims: list[Claim]) -> float:
    qs = {stem(t) for t in tokens(question) if t not in GENERIC}
    if not qs:
        return 1.0
    have = {stem(t) for c in claims for t in tokens(c.text)}
    have |= {stem(t) for c in claims for ct in c.citations for t in tokens(ct.source_title)}
    return len(qs & have) / len(qs)


def decide_mode(claims: list[Claim], dropped: int, conflicts: list[ConflictNote], coverage: float) -> AnswerMode:
    if not claims:
        return "insufficient"
    if conflicts:
        return "conflicting"
    if dropped > 0 or coverage < 0.5:
        return "partial"
    return "supported"


def render_answer(mode: AnswerMode, claims: list[Claim], conflicts: list[ConflictNote], generation_mode: str) -> str:
    if mode == "insufficient":
        return ("The workspace does not contain enough evidence to answer this question. "
                "I did not find relevant passages in the added sources, so I will not guess.")
    head = {
        "supported": "Supported by the cited sources:",
        "partial": "Partially supported — only some parts of the question are covered by the sources:",
        "conflicting": "Conflicting evidence — sources disagree or may disagree:",
    }[mode]
    lines = [head]
    n = 0
    for c in claims:
        n += 1
        src = "; ".join(sorted({f"{x.source_title}" + (f", p.{x.page}" if x.page else "") for x in c.citations}))
        tag = {"inference": " (inference — not stated by a source)", "user_note": " (from your notes)", "uncertain": " (uncertain)"}.get(c.kind, "")
        lines.append(f"{n}. {c.text}{tag} [{src}]")
    for cf in conflicts:
        lines.append(f"⚠ {cf.description} ({cf.status})")
    return "\n".join(lines)
