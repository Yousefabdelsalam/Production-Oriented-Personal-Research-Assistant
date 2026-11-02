"""Cross-source relationship proposals (heuristic + LLM) and their deterministic validation.

Hard rules:
  * relationships connect two VALIDATED findings from DIFFERENT sources, with both passages as evidence
  * co-occurrence in a workspace is never enough: a pair needs real topical overlap before it can be proposed at all
  * the heuristic strategy can only produce status='hypothesis'; 'supported' needs LLM proposal + deterministic corroboration
    (or explicit user confirmation through the API)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.harness.policies import PREDICATES
from app.models import ExtractedFinding
from app.services.llm import LLMClient, Usage, with_rules, wrap_untrusted
from app.services.textutil import jaccard, norm_for_match, numbers_in, stem, tokens, truncate

SYMMETRIC = {"supports", "contradicts"}
_NEG_WORDS = {"not", "no", "never", "cannot", "without", "neither", "nor", "fails", "fail", "unable", "lack", "lacks", "unreproducible"}
_NEG_PHRASES = re.compile(r"\b(could not|couldn't|cannot|can't|did not|does not|do not|not (be )?reproduc\w+|fail(s|ed)? to|no (significant|measurable|improvement))\b", re.I)
_ANTONYMS = [({"reduc", "decre", "lower", "drop", "declin"}, {"incre", "raise", "boost", "highe", "grow"}),
             ({"impro", "outpe", "bette", "enhan"}, {"degra", "worse", "under", "hurt", "harm"})]
_GENERIC = {"result", "benchmark", "baseline", "model", "method", "approach", "system", "paper", "using"}


@dataclass
class Pair:
    a: ExtractedFinding
    b: ExtractedFinding
    shared: list[str]
    overlap: float
    score: float


@dataclass
class Proposal:
    a_id: str
    b_id: str
    predicate: str
    rationale: str
    detected_by: str  # heuristic | llm
    confidence: float = 0.5
    corroborated: bool = False


def _core(f: ExtractedFinding) -> set[str]:
    return {stem(t) for t in tokens(f.statement) if len(t) > 3}


def _negated(text: str) -> bool:
    return bool(_NEG_PHRASES.search(text)) or any(t in _NEG_WORDS for t in tokens(text, keep_stop=True))


def _concept_stems(f: ExtractedFinding) -> set[str]:
    return {stem(w) for c in (f.concepts or []) for w in tokens(c) if len(w) > 3}


def candidate_pairs(findings: list[ExtractedFinding], focus_source_id: str | None, limit: int = 60) -> list[Pair]:
    """Cheap, deterministic topical-overlap filter. Only pairs that pass it can ever become a relationship."""
    cores = {f.id: _core(f) | _concept_stems(f) for f in findings}
    pairs: list[Pair] = []
    for i, a in enumerate(findings):
        for b in findings[i + 1:]:
            if a.source_id == b.source_id:
                continue
            if focus_source_id and focus_source_id not in (a.source_id, b.source_id):
                continue  # incremental: only pairs involving the newly added source
            shared = (cores[a.id] & cores[b.id]) - {stem(g) for g in _GENERIC}
            nums = numbers_in(a.statement) & numbers_in(b.statement)
            jac = jaccard(cores[a.id], cores[b.id])
            if len(shared) >= 3 or (len(shared) >= 2 and nums):
                pairs.append(Pair(a, b, sorted(shared), jac, len(shared) + 2 * jac + (1 if nums else 0)))
    pairs.sort(key=lambda p: -p.score)
    return pairs[:limit]


def _antonym_hit(sa: set[str], sb: set[str]) -> tuple[str, str] | None:
    for left, right in _ANTONYMS:
        la, ra, lb, rb = sa & left, sa & right, sb & left, sb & right
        if (la and rb) or (ra and lb):
            return (sorted(la or ra)[0], sorted(lb or rb)[0])
    return None


def coined_names(findings: list[ExtractedFinding]) -> dict[str, str]:
    """'We propose MemoryBank-Lite, ...' -> {'memorybank-lite': source_id}"""
    out: dict[str, str] = {}
    rx = re.compile(r"\b(?:we|this paper) (?:propose|introduce|present|develop)s?\s+(?:a |an |the )?([A-Z][\w\-]{3,40})", re.I)
    for f in findings:
        if f.kind == "method":
            m = rx.search(f.statement)
            if m and m.group(1)[0].isupper():
                out.setdefault(m.group(1).lower().rstrip(",."), f.source_id)
    return out


def propose_heuristic(findings: list[ExtractedFinding], pairs: list[Pair]) -> list[Proposal]:
    out: list[Proposal] = []
    seen: set[tuple[str, str, str]] = set()

    def add(a: ExtractedFinding, b: ExtractedFinding, pred: str, why: str, conf: float, corr: bool = False) -> None:
        if pred in SYMMETRIC and a.id > b.id:
            a, b = b, a
        k = (a.id, b.id, pred)
        if k not in seen:
            seen.add(k)
            out.append(Proposal(a.id, b.id, pred, why, "heuristic", conf, corr))

    for p in pairs:
        a, b = p.a, p.b
        na, nb = _negated(a.statement), _negated(b.statement)
        sa, sb = _core(a), _core(b)
        anto = _antonym_hit(sa, sb)
        nums_a, nums_b = numbers_in(a.statement), numbers_in(b.statement)
        shared_topic = ", ".join(p.shared[:5])
        if na != nb:
            neg_side = a if na else b
            add(a, b, "contradicts",
                f"Heuristic: the two statements share topic terms ({shared_topic}) but one is negated ('{truncate(neg_side.statement, 90)}') "
                "while the other is affirmative.", 0.6, corr=True)
        elif anto and not (na or nb):
            add(a, b, "contradicts", f"Heuristic: shared topic ({shared_topic}) with opposite directions ('{anto[0]}' vs '{anto[1]}').", 0.5, corr=True)
        elif nums_a and nums_b and not (nums_a & nums_b) and len(p.shared) >= 3 and a.kind == b.kind == "result":
            add(a, b, "contradicts",
                f"Heuristic: both report a figure for a similar measure ({shared_topic}) but values differ "
                f"({', '.join(sorted(nums_a))} vs {', '.join(sorted(nums_b))}); this may reflect different setups, so check before concluding.", 0.35)
        elif p.overlap >= 0.45 and not (na or nb) and (nums_a & nums_b or p.overlap >= 0.6):
            add(a, b, "supports", f"Heuristic: near-identical content ({shared_topic}) with the same polarity in two sources.", 0.45)

    names = coined_names(findings)
    by_source: dict[str, list[ExtractedFinding]] = {}
    for f in findings:
        by_source.setdefault(f.source_id, []).append(f)
    for name, src in names.items():
        origin = next((f for f in by_source.get(src, []) if f.kind == "method" and name in f.statement.lower()), None)
        for f in findings:
            if f.source_id == src or origin is None:
                continue
            if re.search(rf"\b{re.escape(name)}\b", f.statement, re.I):
                pred = "uses"
                add(f, origin, pred, f"Heuristic: this statement names '{name}', a method introduced in the other source.", 0.4)
    return out


# ---------------------------------------------------------------------------------------------
class _Rel(BaseModel):
    a: str
    b: str
    predicate: str
    rationale: str = ""
    confidence: float = 0.5


class _Rels(BaseModel):
    relationships: list[_Rel] = Field(default_factory=list)


REL_SYSTEM = (
    "You compare pairs of research findings from DIFFERENT sources. For each pair decide whether there is a relationship "
    f"clearly supported by the two statements alone. Allowed predicates: {', '.join(PREDICATES)} "
    "('contradicts' = they make incompatible claims about the same thing; 'supports' = independent agreement; 'extends' = B builds on A; "
    "'uses' = B uses a method/result of A; 'depends_on'; 'applies_to'). Return ONLY relationships you can justify in one sentence "
    "citing both statements. Do NOT relate findings merely because they share a topic. Return an empty list if unsure."
)


def propose_llm(llm: LLMClient, pairs: list[Pair], titles: dict[str, str]) -> tuple[list[Proposal], list[Usage]]:
    usages: list[Usage] = []
    out: list[Proposal] = []
    for i in range(0, min(len(pairs), 40), 20):
        chunk = pairs[i:i + 20]
        labels: dict[str, ExtractedFinding] = {}
        blocks = []
        for n, p in enumerate(chunk):
            for side, f in (("a", p.a), ("b", p.b)):
                lab = f"F{n + 1}{side.upper()}"
                labels[lab] = f
                blocks.append(wrap_untrusted(f"{lab} source=\"{titles.get(f.source_id, '')}\"", f.statement))
        parsed, usage = llm.complete_json(purpose="propose_relationships", system=with_rules(REL_SYSTEM),
                                          user="Pairs to consider: " + ", ".join(f"(F{n + 1}A, F{n + 1}B)" for n in range(len(chunk))) + "\n\n" + "\n".join(blocks),
                                          schema=_Rels, max_tokens=1800)
        usages.append(usage)
        for r in parsed.relationships:
            fa, fb = labels.get(r.a.strip().upper()), labels.get(r.b.strip().upper())
            if fa is None or fb is None or r.predicate not in PREDICATES:
                continue
            if fa.id > fb.id and r.predicate in SYMMETRIC:
                fa, fb = fb, fa
            out.append(Proposal(fa.id, fb.id, r.predicate, r.rationale.strip(), "llm", max(0.0, min(1.0, r.confidence))))
    return out, usages


# ---------------------------------------------------------------------------------------------
@dataclass
class RelVerdict:
    ok: bool
    status: str  # supported | hypothesis
    reasons: list[str] = field(default_factory=list)


def validate_proposal(p: Proposal, by_id: dict[str, ExtractedFinding]) -> RelVerdict:
    a, b = by_id.get(p.a_id), by_id.get(p.b_id)
    reasons: list[str] = []
    if a is None or b is None:
        return RelVerdict(False, "hypothesis", ["finding does not exist or is not validated"])
    if a.id == b.id or a.source_id == b.source_id:
        reasons.append("relationships must connect findings from different sources")
    if p.predicate not in PREDICATES:
        reasons.append(f"unknown predicate '{p.predicate}'")
    if not a.passage_id or not b.passage_id:
        reasons.append("missing evidence passages")
    if not p.rationale or len(p.rationale) < 15:
        reasons.append("missing rationale")
    shared = ((_core(a) | _concept_stems(a)) & (_core(b) | _concept_stems(b))) - {stem(g) for g in _GENERIC}
    named = p.predicate == "uses" and p.detected_by == "heuristic"
    if len(shared) < 2 and not named:
        reasons.append("no real topical overlap between the two findings — relationship would be invented")
    if reasons:
        return RelVerdict(False, "hypothesis", reasons)
    # status: heuristics never reach 'supported'; LLM proposals need independent deterministic corroboration
    corroborated = False
    if p.predicate == "contradicts":
        corroborated = _negated(a.statement) != _negated(b.statement) or _antonym_hit(_core(a), _core(b)) is not None
    elif p.predicate == "supports":
        corroborated = not (_negated(a.statement) or _negated(b.statement)) and len(shared) >= 3
    else:
        # extends / uses / depends_on / applies_to are semantic: no deterministic check can corroborate them,
        # so they stay hypotheses until a person confirms them.
        corroborated = False
    status = "supported" if (p.detected_by == "llm" and corroborated) else "hypothesis"
    return RelVerdict(True, status, [])
