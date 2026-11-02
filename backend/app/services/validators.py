"""Deterministic validators used by maker-checker steps. No model calls.

`support_score` is a LEXICAL support heuristic (stem overlap + numeric consistency). It catches claims that
introduce content absent from the cited text; it does NOT prove entailment. An optional LLM checker may only
*downgrade* a claim, never upgrade it, and is documented as a second opinion, not a guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.textutil import norm_for_match, numbers_in, stem, tokens


def quote_in_text(quote: str, text: str) -> bool:
    """True iff `quote` is a verbatim (whitespace/quote-style-insensitive) substring of `text`."""
    q = norm_for_match(quote).strip(" .…\"'")
    return bool(q) and q in norm_for_match(text)


@dataclass
class SupportResult:
    score: float
    missing_terms: list[str] = field(default_factory=list)
    missing_numbers: list[str] = field(default_factory=list)

    @property
    def numbers_ok(self) -> bool:
        return not self.missing_numbers


NEGATIONS = {"not", "no", "never", "cannot", "without", "neither", "nor", "n't", "fails", "fail", "unable", "lack", "lacks"}


def _negated(tokens_: list[str]) -> bool:
    return any(t in NEGATIONS or t.endswith("n't") for t in tokens_)


def support_score(claim: str, evidence_texts: list[str]) -> SupportResult:
    claim_terms = {stem(t) for t in tokens(claim)}
    if not claim_terms:
        return SupportResult(0.0)
    ev_joined = " ".join(evidence_texts)
    ev_terms = {stem(t) for t in tokens(ev_joined)}
    missing = sorted(claim_terms - ev_terms)
    score = 1 - len(missing) / len(claim_terms)
    claim_nums, ev_nums = numbers_in(claim), numbers_in(ev_joined)
    missing_nums = sorted(n for n in claim_nums if n not in ev_nums)
    # polarity: a claim that asserts the opposite polarity of every cited sentence is not supported
    ctoks = tokens(claim, keep_stop=True)
    etoks = tokens(ev_joined, keep_stop=True)
    if _negated(ctoks) != _negated(etoks) and score < 0.95:
        score = min(score, 0.4)
        missing.append("<polarity mismatch>")
    return SupportResult(round(score, 3), missing[:12], missing_nums)


SUPPORT_THRESHOLD = 0.6
