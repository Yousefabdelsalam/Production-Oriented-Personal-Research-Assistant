"""Workspace-scoped evidence retrieval: FTS5 BM25 (+ optional embedding cosine, fused by RRF), source filters,
overlap/near-duplicate removal and per-source diversification. Every hit explains why it was returned."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import EvidencePassage, Source
from app.services.llm import Embedder, cosine, pack_vec, unpack_vec
from app.services.textutil import jaccard, stem, tokens


class EvidenceHit(BaseModel):
    passage_id: str
    source_id: str
    source_title: str
    source_kind: str
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    text: str
    score: float
    lexical_score: float = 0.0
    coverage: float = 0.0  # fraction of the query's content terms present in this passage
    vector_score: float | None = None
    suspicious: bool = False
    reasons: list[str] = []
    also_in: list[str] = []  # near-duplicate passage ids folded into this hit


def index_passages(session: Session, passages: list[EvidencePassage]) -> None:
    for p in passages:
        session.execute(
            text("INSERT INTO passages_fts(text, passage_id, workspace_id, source_id) VALUES (:t, :p, :w, :s)"),
            # section heading is indexed with the text so "limitations of X" can find a passage under "3. Limitations"
            {"t": f"{p.section}\n{p.text}" if p.section else p.text, "p": p.id, "w": p.workspace_id, "s": p.source_id},
        )


def remove_from_index(session: Session, passage_ids: list[str]) -> None:
    for pid in passage_ids:
        session.execute(text("DELETE FROM passages_fts WHERE passage_id = :p"), {"p": pid})


def embed_passages(session: Session, embedder: Embedder, passages: list[EvidencePassage]) -> int:
    if not embedder.enabled or not passages:
        return 0
    vecs = embedder.embed([p.text for p in passages])
    for p, v in zip(passages, vecs):
        p.embedding = pack_vec(v)
    return len(passages)


def _fts_query(question: str) -> str | None:
    toks = list(dict.fromkeys(tokens(question)))[:24]
    toks = [t.replace('"', "") for t in toks if t]
    return " OR ".join(f'"{t}"' for t in toks) if toks else None


def _lexical(session: Session, workspace_id: str, query: str, limit: int, source_ids: list[str] | None) -> list[tuple[str, float]]:
    q = _fts_query(query)
    if not q:
        return []
    sql = (
        "SELECT passage_id, bm25(passages_fts) AS r FROM passages_fts "
        "WHERE passages_fts MATCH :q AND workspace_id = :w "
    )
    params: dict = {"q": q, "w": workspace_id, "n": limit}
    if source_ids:
        keys = {f"s{i}": s for i, s in enumerate(source_ids)}
        sql += "AND source_id IN (" + ",".join(f":{k}" for k in keys) + ") "
        params.update(keys)
    sql += "ORDER BY r LIMIT :n"
    return [(row[0], -float(row[1])) for row in session.execute(text(sql), params).all()]


def _overlap(a: EvidencePassage, b: EvidencePassage) -> float:
    if a.source_version_id != b.source_version_id:
        return 0.0
    lo, hi = max(a.char_start, b.char_start), min(a.char_end, b.char_end)
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1, min(a.char_end - a.char_start, b.char_end - b.char_start))


@dataclass
class _Cand:
    p: EvidencePassage
    lex: float = 0.0
    vec: float | None = None
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    also: list[str] = field(default_factory=list)
    coverage: float = 0.0


def search_evidence(
    session: Session,
    workspace_id: str,
    query: str,
    *,
    k: int = 8,
    source_ids: list[str] | None = None,
    embedder: Embedder | None = None,
    max_per_source: int | None = None,
    boost_sources: dict[str, float] | None = None,
) -> list[EvidenceHit]:
    """`boost_sources` lets memory recall nudge ranking toward sources it points at (explained in `reasons`)."""
    lex = _lexical(session, workspace_id, query, limit=60, source_ids=source_ids)
    cands: dict[str, _Cand] = {}
    qstems = {stem(t) for t in tokens(query)}
    top = max((s for _, s in lex), default=0.0) or 1.0
    ids = [pid for pid, _ in lex]
    rows = {p.id: p for p in session.query(EvidencePassage).filter(EvidencePassage.id.in_(ids), EvidencePassage.is_current.is_(True), EvidencePassage.workspace_id == workspace_id).all()} if ids else {}
    src_titles = {s.id: s.title for s in session.query(Source).filter(Source.id.in_({p.source_id for p in rows.values()})).all()} if rows else {}
    for pid, s in lex:
        p = rows.get(pid)
        if p is None:
            continue
        # naming the source ("limitations of X") or its section counts as covering those query terms
        ptoks = {stem(t) for t in tokens(p.text + " " + (p.section or "") + " " + src_titles.get(p.source_id, ""))}
        hit_terms = sorted(qstems & ptoks)
        coverage = len(hit_terms) / max(1, len(qstems))
        c = _Cand(p=p, lex=0.6 * (s / top) + 0.4 * coverage, coverage=coverage)
        c.reasons.append(f"lexical match on {', '.join(hit_terms[:6]) or 'terms'} (coverage {coverage:.0%})")
        cands[pid] = c

    if embedder is not None and embedder.enabled:
        qv = embedder.embed([query])[0]
        q = session.query(EvidencePassage).filter(
            EvidencePassage.workspace_id == workspace_id, EvidencePassage.is_current.is_(True), EvidencePassage.embedding.is_not(None)
        )
        if source_ids:
            q = q.filter(EvidencePassage.source_id.in_(source_ids))
        scored = sorted(((cosine(qv, unpack_vec(p.embedding)), p) for p in q.all()), key=lambda x: -x[0])[:60]
        lex_rank = {pid: i for i, (pid, _) in enumerate(lex)}
        for rank, (cs, p) in enumerate(scored):
            c = cands.setdefault(p.id, _Cand(p=p))
            if c.coverage == 0.0:
                c.coverage = len(qstems & {stem(t) for t in tokens(p.text + " " + (p.section or ""))}) / max(1, len(qstems))
            c.vec = cs
            c.reasons.append(f"semantic similarity {cs:.2f}")
            # reciprocal rank fusion
            c.score = 1 / (60 + lex_rank.get(p.id, 1000)) + 1 / (60 + rank)
        for pid, c in cands.items():
            if c.vec is None:
                c.score = 1 / (60 + lex_rank.get(pid, 1000))
        mx = max((c.score for c in cands.values()), default=1.0) or 1.0
        for c in cands.values():
            c.score = c.score / mx
    else:
        for c in cands.values():
            c.score = c.lex

    if not cands:
        return []
    titles = {s.id: s for s in session.query(Source).filter(Source.id.in_({c.p.source_id for c in cands.values()})).all()}
    for c in cands.values():
        src = titles.get(c.p.source_id)
        if src is not None and qstems & {stem(t) for t in tokens(src.title)}:
            c.score += 0.08
            c.reasons.append("query terms appear in the source title")
        if c.p.section and qstems & {stem(t) for t in tokens(c.p.section)}:
            c.score += 0.08
            c.reasons.append(f"query terms appear in the section heading '{c.p.section[:40]}'")
        if boost_sources and c.p.source_id in boost_sources:
            c.score += boost_sources[c.p.source_id]
            c.reasons.append("source suggested by recalled memory")

    ordered = sorted(cands.values(), key=lambda c: -c.score)
    kept: list[_Cand] = []
    for c in ordered:
        dup = None
        ctoks = set(tokens(c.p.text))
        for kc in kept:
            if _overlap(c.p, kc.p) >= 0.4 or (kc.p.source_id != c.p.source_id and jaccard(ctoks, set(tokens(kc.p.text))) >= 0.85):
                dup = kc
                break
        if dup is not None:
            dup.also.append(c.p.id)
            continue
        if max_per_source and sum(1 for kc in kept if kc.p.source_id == c.p.source_id) >= max_per_source:
            continue
        kept.append(c)
        if len(kept) >= k:
            break

    out = []
    for c in kept:
        src = titles.get(c.p.source_id)
        out.append(
            EvidenceHit(
                passage_id=c.p.id, source_id=c.p.source_id, source_title=src.title if src else "", source_kind=src.kind if src else "",
                page_start=c.p.page_start, page_end=c.p.page_end, section=c.p.section, text=c.p.text,
                score=round(c.score, 4), lexical_score=round(c.lex, 4), coverage=round(c.coverage, 3),
                vector_score=None if c.vec is None else round(c.vec, 4), suspicious=c.p.suspicious,
                reasons=c.reasons, also_in=c.also,
            )
        )
    return out
