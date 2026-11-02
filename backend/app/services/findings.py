"""Research-finding extraction (strategy: deterministic 'extractive' or LLM) and deterministic validation.

A finding always carries: kind, statement, a verbatim quote from ONE passage, epistemic label, concepts.
Extraction proposes; `validate_finding` decides. Instruction-like sentences from documents are never proposed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.harness.injection import scan_text
from app.models import EvidencePassage
from app.services.llm import LLMClient, Usage, with_rules, wrap_untrusted
from app.services.textutil import norm_for_match, sentences, sha256_text, stem, tokens
from app.services.validators import SUPPORT_THRESHOLD, quote_in_text, support_score

KINDS = ("finding", "definition", "method", "result", "limitation", "open_question")
EPISTEMICS = ("source_stated", "user_note", "inference", "uncertain")


class FindingDraft(BaseModel):
    passage_id: str
    kind: str = "finding"
    statement: str
    quote: str
    epistemic: str = "source_stated"
    concepts: list[str] = Field(default_factory=list)
    confidence: float = 0.5


class _LLMFinding(BaseModel):
    passage: str  # "P3" label
    kind: str = "finding"
    statement: str
    quote: str
    epistemic: str = "source_stated"
    concepts: list[str] = Field(default_factory=list)
    confidence: float = 0.6


class _LLMFindings(BaseModel):
    findings: list[_LLMFinding] = Field(default_factory=list)


_CUES: list[tuple[str, re.Pattern[str], float]] = [
    ("definition", re.compile(r"\b(is defined as|are defined as|we define|refers? to|is a (type|form|class|kind) of|is known as|denotes?)\b", re.I), 2.0),
    ("method", re.compile(r"\b(we (propose|introduce|present|develop|design|use)|our (method|approach|framework|system|model)|is based on|is built on|relies on|consists of|works by)\b", re.I), 1.6),
    ("limitation", re.compile(r"\b(limitations?|however|does not|do not|cannot|fails? to|only (covers|evaluated|tested)|restricted to|not evaluated|struggles?|unable to|shortcomings?|drawbacks?)\b", re.I), 1.5),
    ("open_question", re.compile(r"\b(remains? (an )?(open|unclear|unknown)|open (question|problem|challenge)s?|future work|it is unclear|not yet (known|understood))\b", re.I), 1.8),
    ("result", re.compile(r"\b(outperform\w*|improv\w+|reduc\w+|increas\w+|decreas\w+|achiev\w+|accuracy|precision|recall|f1|latency|speed-?up|throughput|error rate|hallucination)\b", re.I), 1.4),
    ("finding", re.compile(r"\b(we (find|found|show|showed|observe|observed|demonstrate|report)|results? (show|suggest|indicate)|demonstrates?|reveals?|suggests?|indicates?|evidence)\b", re.I), 1.2),
]
_NUM = re.compile(r"\d")
_OPINION = re.compile(r"\b(i (?:\w+ )?(think|believe|suspect|expect|doubt|assume|wonder|feel|guess)|my (hypothesis|view|take|guess)|open question|question:|we should|should be|might|probably)\b", re.I)
_ACRONYM = re.compile(r"\b[A-Z][A-Za-z]*[A-Z][A-Za-z]*\b|\b[A-Z]{2,}\b")
_CAP_PHRASE = re.compile(r"\b(?:[A-Z][a-z]+(?:[- ][A-Za-z]+){0,3})\b")
_HYPH = re.compile(r"\b[a-z]+(?:-[a-z]+)+\b")


def concept_candidates(sentence: str, limit: int = 6) -> list[str]:
    cands: list[str] = []
    for rx in (_ACRONYM, _HYPH):
        cands += rx.findall(sentence)
    for m in _CAP_PHRASE.findall(sentence):
        if m.split()[0].lower() not in {"the", "this", "that", "we", "our", "in", "on", "it", "however", "these", "a", "an"} and len(m) > 3:
            cands.append(m)
    toks = [t for t in tokens(sentence) if len(t) > 5]
    cands += toks[:3]
    seen, out = set(), []
    for c in cands:
        k = norm_for_match(c)
        if k not in seen and len(k) > 2:
            seen.add(k)
            out.append(k)
    return out[:limit]


def classify_sentence(s: str) -> tuple[str, float]:
    best_kind, best = "finding", 0.0
    total = 0.0
    for kind, rx, w in _CUES:
        if rx.search(s):
            total += w
            if w > best:
                best_kind, best = kind, w
    if _NUM.search(s):
        total += 0.6
        if best_kind == "finding" and best == 0:
            best_kind = "result"
    return best_kind, total


def extract_findings_extractive(passages: list[EvidencePassage], *, is_note: bool, max_findings: int = 40) -> list[FindingDraft]:
    scored: list[tuple[float, FindingDraft]] = []
    seen: set[str] = set()
    for p in passages:
        if p.suspicious and not is_note:
            pass  # sentences are individually screened below
        for sent in sentences(p.text):
            if not (40 <= len(sent) <= 420) or not sent.rstrip().endswith((".", "!", "?")) or len(sent.split()) < 6:
                continue  # headings, titles and fragments are not findings
            if scan_text(sent):
                continue  # instruction-like sentence: never becomes a finding
            kind, score = classify_sentence(sent)
            if is_note and _OPINION.search(sent):
                score += 1.2  # the user's own beliefs / hypotheses / open questions are worth keeping
                if kind == "finding" and re.search(r"\b(open question|question:|how (should|can|do))\b", sent, re.I):
                    kind = "open_question"
            if score < 1.2:
                continue  # no research cue: e.g. logistics notes yield nothing
            key = sha256_text(norm_for_match(sent))
            if key in seen:
                continue
            seen.add(key)
            scored.append((score, FindingDraft(
                passage_id=p.id, kind=kind, statement=sent, quote=sent,
                epistemic="user_note" if is_note else "source_stated",
                concepts=concept_candidates(sent), confidence=round(min(0.9, 0.35 + score / 5), 2),
            )))
    scored.sort(key=lambda t: -t[0])
    return [d for _, d in scored[:max_findings]]


LLM_SYSTEM = (
    "You extract research findings from numbered passages of one source document. For each important finding, definition, "
    "method, reported result, limitation or open question return: passage label (e.g. P3), kind "
    "(finding|definition|method|result|limitation|open_question), a concise `statement`, a `quote` copied VERBATIM from "
    "that passage (a contiguous span), `concepts` (1-5 short noun phrases), `epistemic` ('source_stated' if the passage states it, "
    "'inference' only if you are concluding something the passage does not literally say), and `confidence` 0-1. "
    "Do not invent facts. Skip anything not clearly present."
)


def extract_findings_llm(llm: LLMClient, passages: list[EvidencePassage], title: str, *, is_note: bool,
                         max_batches: int = 6, batch_chars: int = 9000) -> tuple[list[FindingDraft], list[Usage]]:
    usages: list[Usage] = []
    drafts: list[FindingDraft] = []
    batches: list[list[EvidencePassage]] = [[]]
    size = 0
    for p in passages:
        if size + len(p.text) > batch_chars and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(p)
        size += len(p.text)
    for batch in batches[:max_batches]:
        labels = {f"P{i + 1}": p for i, p in enumerate(batch)}
        body = "\n\n".join(
            wrap_untrusted(f"{lab} page={p.page_start} section={p.section or '-'}", p.text) for lab, p in labels.items()
        )
        parsed, usage = llm.complete_json(
            purpose="extract_findings", system=with_rules(LLM_SYSTEM),
            user=f"Source title: {title}\n\n{body}", schema=_LLMFindings, max_tokens=2500,
        )
        usages.append(usage)
        for f in parsed.findings:
            p = labels.get(f.passage.strip().upper())
            if p is None or f.kind not in KINDS:
                continue
            drafts.append(FindingDraft(
                passage_id=p.id, kind=f.kind, statement=f.statement.strip(), quote=f.quote.strip(),
                epistemic="user_note" if is_note else (f.epistemic if f.epistemic in EPISTEMICS else "source_stated"),
                concepts=[norm_for_match(c) for c in f.concepts][:6], confidence=max(0.0, min(1.0, f.confidence)),
            ))
    return drafts, usages


@dataclass
class FindingVerdict:
    ok: bool
    reasons: list[str]
    support: float


def validate_finding(d: FindingDraft, passage: EvidencePassage | None, *, is_note: bool) -> FindingVerdict:
    reasons: list[str] = []
    if passage is None:
        return FindingVerdict(False, ["passage does not exist"], 0.0)
    if d.kind not in KINDS:
        reasons.append(f"unknown kind '{d.kind}'")
    if d.epistemic not in EPISTEMICS:
        reasons.append(f"unknown epistemic label '{d.epistemic}'")
    if is_note and d.epistemic != "user_note":
        reasons.append("note-derived statements must be labelled user_note")
    if not is_note and d.epistemic == "user_note":
        reasons.append("user_note label on a non-note source")
    if not quote_in_text(d.quote, passage.text):
        reasons.append("quote is not a verbatim excerpt of the cited passage")
    if scan_text(d.statement) or scan_text(d.quote):
        reasons.append("instruction-like text")
    if not (20 <= len(d.statement) <= 700):
        reasons.append("statement length out of bounds")
    sup = support_score(d.statement, [d.quote])
    if d.epistemic != "inference" and (sup.score < SUPPORT_THRESHOLD or not sup.numbers_ok):
        reasons.append(f"statement not supported by its quote (score {sup.score}, missing numbers {sup.missing_numbers})")
    if d.epistemic == "inference" and sup.score < 0.35:
        reasons.append("inference is not anchored in the quote")
    return FindingVerdict(not reasons, reasons, sup.score)
