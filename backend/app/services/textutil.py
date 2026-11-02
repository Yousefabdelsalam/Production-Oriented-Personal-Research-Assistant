"""Small, dependency-free text helpers shared by chunking, retrieval, validators and extractive strategies."""
from __future__ import annotations

import hashlib
import re
import unicodedata

STOPWORDS = frozenset(
    """a about above after again against all also am an and any are aren't as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have having he her here
    hers him his how i if in into is it its itself just me more most my no nor not of off on once only or other our out over
    own same she should so some such than that the their theirs them then there these they this those through to too under
    until up very was we were what when where which while who whom why will with would you your yours paper papers study
    studies using used use based show shows shown results result approach""".split()
)

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-_.']*[a-z0-9]|[a-z0-9]")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def squash_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def norm_for_match(s: str) -> str:
    """Casefold, unify quotes/dashes, de-hyphenate line wraps, collapse whitespace."""
    s = unicodedata.normalize("NFKC", s).translate(_QUOTE_MAP)
    s = re.sub(r"-\s*\n\s*", "", s)
    return squash_ws(s).casefold()


_SUFFIXES = ("ations", "ation", "tions", "tion", "ments", "ment", "ingly", "edly", "ings", "ing", "ies", "ied", "ed", "es", "s", "e")


def stem(tok: str) -> str:
    """Deliberately crude stemmer used only for overlap/coverage/support heuristics (retrieval uses FTS5's Porter).
    Strips common suffixes, then keeps a 5-char prefix so 'reduce/reduces/reduced/reduction' and
    'hallucinate/hallucination' compare equal."""
    if tok.isdigit() or len(tok) <= 4:
        return tok
    for suf in _SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            tok = tok[: -len(suf)]
            break
    return tok[:5]


def tokens(s: str, keep_stop: bool = False) -> list[str]:
    toks = _TOKEN.findall(norm_for_match(s))
    if keep_stop:
        return toks
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def numbers_in(s: str) -> set[str]:
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*\.?\d*%?", s)}


def sentences(text: str) -> list[str]:
    text = squash_ws(text)
    if not text:
        return []
    return [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def truncate(s: str, n: int = 240) -> str:
    s = squash_ws(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def slugify(s: str, max_len: int = 48) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:max_len].strip("-") or "workspace"
