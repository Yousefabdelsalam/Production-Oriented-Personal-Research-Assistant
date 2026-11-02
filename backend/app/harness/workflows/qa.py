"""Workflow B — research question answering.

QUESTION_RECEIVED → CLASSIFY_INTENT → RESOLVE_WORKSPACE → (RECALL_HINDSIGHT_MEMORY) → RETRIEVE_SOURCE_EVIDENCE
  → MERGE_AND_RANK_EVIDENCE → GENERATE_GROUNDED_ANSWER → VALIDATE_CITATIONS_AND_CLAIMS → RETURN_ANSWER
  → RECORD_USEFUL_EXPERIENCE

The harness decides what to fetch (plan), the model only drafts claims, and a deterministic validator + optional
LLM second opinion decide what survives. Memory outages degrade the answer (with a warning) instead of failing it.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.errors import InvalidInput, NotFound, RetryableError, AppError
from app.harness.engine import RunContext, Stage, StageResult, WorkflowDef
from app.models import ChatTurn, Conversation, ExtractedFinding, ResearchRelationship, Source, Workspace
from app.schemas import AnswerResult, ConflictNote, MemoryUsed
from app.services import qa as qa_svc
from app.services.memory import MemoryService
from app.services.retrieval import EvidenceHit, search_evidence
from app.services.textutil import numbers_in, stem, tokens, truncate

log = logging.getLogger("qa")
MIN_COVERAGE = 0.34


class QAState(BaseModel):
    workspace_id: str
    question: str
    conversation_id: str | None = None
    plan: dict[str, Any] = Field(default_factory=dict)
    sources_in_workspace: int = 0
    memories: list[dict[str, Any]] = Field(default_factory=list)  # RecalledMemory dumps
    memory_status: str = "not_attempted"  # skipped | ok | unavailable | not_attempted
    hits: list[dict[str, Any]] = Field(default_factory=list)  # EvidenceHit dumps (merged, ranked)
    raw_hit_count: int = 0
    overview: list[dict[str, Any]] = Field(default_factory=list)
    drafts: list[dict[str, Any]] = Field(default_factory=list)
    final: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    experience: dict[str, Any] = Field(default_factory=dict)


def _hits(st: QAState) -> list[EvidenceHit]:
    return [EvidenceHit.model_validate(h) for h in st.hits]


def question_received(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    q = st.question.strip()
    if not q:
        raise InvalidInput("Question is empty.", code="empty_question")
    if len(q) > 2000:
        raise InvalidInput("Question is too long (max 2000 characters).", code="question_too_long")
    st.question = q
    ctx.trace("trigger", {"question_chars": len(q), "conversation": st.conversation_id})
    return StageResult("CLASSIFY_INTENT")


def classify_intent(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    srcs = [(s.id, s.title, s.kind) for s in ctx.session.query(Source).filter(
        Source.workspace_id == st.workspace_id, Source.status.in_(("completed", "partial")), Source.current_version > 0,
        Source.duplicate_of.is_(None)).all()]
    previous: list[dict] = []
    if st.conversation_id:
        turns = (ctx.session.query(ChatTurn).filter_by(conversation_id=st.conversation_id)
                 .order_by(ChatTurn.created_at.desc()).limit(3).all())
        for t in reversed(turns):
            cited = sorted({c["source_id"] for cl in (t.answer or {}).get("claims", []) for c in cl.get("citations", [])})
            previous.append({"question": t.question, "source_ids": cited})
    plan = qa_svc.classify_question(st.question, srcs, previous)
    st.plan = plan.as_dict()
    st.sources_in_workspace = len(srcs)
    ctx.trace("plan", {"intent": plan.intent, "rationale": plan.rationale, "source_filter": plan.source_ids, "evidence_k": plan.evidence_k,
                       "use_memory": plan.use_memory, "memory_budget": plan.memory_budget, "followup_context": bool(previous)})
    return StageResult("RESOLVE_WORKSPACE")


def resolve_workspace(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    if ws is None:
        raise NotFound(f"workspace {st.workspace_id} not found")
    ctx.trace("workspace", {"id": ws.id, "bank": ws.bank_id, "sources": st.sources_in_workspace})
    if st.sources_in_workspace == 0:
        st.warnings.append("This workspace has no processed sources yet.")
        st.memory_status = "skipped"
        return StageResult("RETURN_ANSWER")  # guarded branch: nothing to retrieve or generate
    return StageResult("RECALL_HINDSIGHT_MEMORY")


def recall_hindsight_memory(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    plan = st.plan
    if not plan.get("use_memory"):
        st.memory_status = "skipped"
        ctx.trace("memory", {"skipped": "plan does not need memory"})
        return StageResult("RETRIEVE_SOURCE_EVIDENCE")
    ws = ctx.session.get(Workspace, st.workspace_id)
    try:
        scoped = plan.get("source_ids") if plan.get("memory_scoped_to_sources") else None
        mems = MemoryService(ctx).recall(ws, plan.get("expanded_query") or st.question, source_ids=scoped,
                                         max_tokens=1200 if plan["memory_budget"] == "low" else 2000, budget=plan["memory_budget"])
        st.memories = [m.__dict__ for m in mems[:8]]
        st.memory_status = "ok"
        ctx.trace("memory", {"returned": len(mems), "kept": len(st.memories), "scoped_to": scoped,
                             "why": "plan requested " + plan["memory_budget"] + "-budget recall for intent " + plan["intent"]})
    except (RetryableError, AppError) as e:
        st.memory_status = "unavailable"
        st.warnings.append(f"Hindsight memory was unavailable; answering from source evidence only ({type(e).__name__}).")
        ctx.trace("memory", {"degraded": True, "error": str(e)[:200]})
    return StageResult("RETRIEVE_SOURCE_EVIDENCE")


def retrieve_source_evidence(ctx: RunContext) -> StageResult:
    ctx.require("read_sources")
    st: QAState = ctx.state
    plan = st.plan
    boost: dict[str, float] = {}
    for m in st.memories:
        for sid in [t.split(":", 1)[1] for t in m.get("tags", []) if t.startswith("source:")]:
            boost[sid] = 0.05
    for sid in plan.get("focus_source_ids", []):
        boost[sid] = boost.get(sid, 0) + 0.15
    hits = search_evidence(
        ctx.session, st.workspace_id, plan.get("expanded_query") or st.question, k=plan["evidence_k"] + 4,
        source_ids=plan.get("source_ids") or None, embedder=ctx.services.models.embedder,
        max_per_source=plan.get("max_per_source"), boost_sources=boost or None,
    )
    if plan.get("intent") == "summary":
        hits = _add_overview_evidence(ctx, st, hits)
    st.raw_hit_count = len(hits)
    st.hits = [h.model_dump() for h in hits]
    ctx.trace("retrieved", [{"passage": h.passage_id, "source": h.source_id, "page": h.page_start, "score": h.score,
                             "coverage": h.coverage, "why": h.reasons} for h in hits])
    return StageResult("MERGE_AND_RANK_EVIDENCE")


_OVERVIEW_KINDS = {"definition": 0, "result": 1, "finding": 2, "method": 3, "limitation": 4, "open_question": 5}


def _add_overview_evidence(ctx: RunContext, st: QAState, hits: list[EvidenceHit]) -> list[EvidenceHit]:
    """Overview questions ("summarize…", "what do we know…") are answered from validated findings, which each carry
    a verbatim quote + passage, rather than from whichever sentences happen to overlap the question words."""
    from app.models import EvidencePassage

    q = ctx.session.query(ExtractedFinding).filter(
        ExtractedFinding.workspace_id == st.workspace_id, ExtractedFinding.validated.is_(True), ExtractedFinding.superseded.is_(False))
    if st.plan.get("source_ids"):
        q = q.filter(ExtractedFinding.source_id.in_(st.plan["source_ids"]))
    findings = sorted(q.all(), key=lambda f: (_OVERVIEW_KINDS.get(f.kind, 9), -f.confidence))
    chosen, per_source = [], {}
    for f in findings:
        if per_source.get(f.source_id, 0) >= (st.plan.get("max_per_source") or 2):
            continue
        per_source[f.source_id] = per_source.get(f.source_id, 0) + 1
        chosen.append(f)
        if len(chosen) >= 6:
            break
    have = {h.passage_id: h for h in hits}
    out = list(hits)
    for f in chosen:
        if f.passage_id not in have:
            p = ctx.session.get(EvidencePassage, f.passage_id)
            src = ctx.session.get(Source, f.source_id)
            if p is None or not p.is_current:
                continue
            h = EvidenceHit(passage_id=p.id, source_id=p.source_id, source_title=src.title, source_kind=src.kind, page_start=p.page_start,
                            page_end=p.page_end, section=p.section, text=p.text, score=0.5, coverage=1.0, suspicious=p.suspicious,
                            reasons=["finding-backed overview evidence"])
            out.append(h)
            have[p.id] = h
    st.overview = [{"passage_id": f.passage_id, "quote": f.quote, "text": f.statement, "kind": "user_note" if f.epistemic == "user_note" else "source_stated"} for f in chosen]
    return out


def merge_and_rank(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    plan = st.plan
    hits = _hits(st)
    kept = [h for h in hits if h.coverage >= MIN_COVERAGE]
    dropped = len(hits) - len(kept)
    kept = kept[: plan["evidence_k"]]
    st.hits = [h.model_dump() for h in kept]
    # memory vs. source discrepancy check (deterministic): numbers in a recalled memory that are absent from the same
    # source's retrieved passages while the texts clearly talk about the same thing => flag, favour the source.
    disc: list[dict] = []
    for m in st.memories:
        sids = [t.split(":", 1)[1] for t in m.get("tags", []) if t.startswith("source:")]
        mtoks = {stem(t) for t in tokens(m["text"])}
        for sid in sids:
            sp = [h for h in kept if h.source_id == sid]
            if not sp:
                continue
            ptoks = {stem(t) for h in sp for t in tokens(h.text)}
            pnums = {n for h in sp for n in numbers_in(h.text)}
            mnums = {n for n in numbers_in(m["text"]) if not (len(n) == 4 and n.startswith("20"))}
            if len(mtoks & ptoks) >= 4 and mnums and not (mnums <= pnums):
                disc.append({"memory": m["id"], "source": sid, "memory_numbers": sorted(mnums), "source_numbers": sorted(pnums)[:10]})
    if disc:
        st.warnings.append("Recalled memory disagrees with the original source on figures; the source passage is used (see memory_used).")
    st.plan["discrepancies"] = disc
    ctx.trace("merge", {"input": len(hits), "dropped_low_coverage": dropped, "kept": len(kept), "discrepancies": disc,
                        "sources": sorted({h.source_id for h in kept})})
    ctx.validation("evidence_nonempty", bool(kept), {"kept": len(kept)})
    return StageResult("GENERATE_GROUNDED_ANSWER")


def generate_grounded_answer(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    hits = _hits(st)
    plan = qa_svc.QAPlan(**{k: v for k, v in st.plan.items() if k in qa_svc.QAPlan.__dataclass_fields__})
    if not hits:
        st.drafts = []
        ctx.trace("generation", {"skipped": "no evidence above relevance threshold"})
        return StageResult("VALIDATE_CITATIONS_AND_CLAIMS")
    llm = ctx.services.models.llm
    if llm is not None:
        mem = [(m["id"], m["text"]) for m in st.memories]
        drafts, usage = qa_svc.generate_llm(llm, st.question, hits, mem, plan)
        ctx.record_usage(usage)
        method = "llm"
    elif st.overview:  # overview intent in extractive mode: claims are the validated findings' own verbatim quotes
        drafts = [qa_svc.DraftClaim(o["quote"], o["kind"], [(o["passage_id"], o["quote"])]) for o in st.overview]
        method = "extractive-overview"
    else:
        drafts, method = qa_svc.generate_extractive(st.question, hits, plan), "extractive"
    st.drafts = [{"text": d.text, "kind": d.kind, "evidence": d.evidence, "memory_refs": d.memory_refs} for d in drafts]
    ctx.trace("generation", {"method": method, "claims_proposed": len(drafts)})
    return StageResult("VALIDATE_CITATIONS_AND_CLAIMS")


def _conflicts_for(ctx: RunContext, st: QAState, claims) -> list[ConflictNote]:
    """Relationships (contradicts) between findings sitting on cited passages; never invents a conflict."""
    cited = {c.passage_id for cl in claims for c in cl.citations}
    if not cited:
        return []
    cited_f = {f.id: f for f in ctx.session.query(ExtractedFinding).filter(
        ExtractedFinding.workspace_id == st.workspace_id, ExtractedFinding.passage_id.in_(cited), ExtractedFinding.superseded.is_(False)).all()}
    if not cited_f:
        return []
    ids = list(cited_f)
    # a contradiction touching ANY cited finding matters, even if its other side was not retrieved for this question
    rels = ctx.session.query(ResearchRelationship).filter(
        ResearchRelationship.workspace_id == st.workspace_id, ResearchRelationship.predicate == "contradicts",
        ResearchRelationship.active.is_(True), ResearchRelationship.status != "rejected",
        (ResearchRelationship.finding_a_id.in_(ids)) | (ResearchRelationship.finding_b_id.in_(ids))).all()
    out = []
    for r in rels:
        sides = []
        for fid in (r.finding_a_id, r.finding_b_id):
            f = cited_f.get(fid) or ctx.session.get(ExtractedFinding, fid)
            src = ctx.session.get(Source, f.source_id)
            sides.append({"source_id": f.source_id, "source_title": src.title if src else "", "passage_id": f.passage_id, "page": f.page,
                          "text": f.quote, "cited": f.passage_id in cited})
        hedge = "" if r.status == "supported" else " (heuristic hypothesis — verify in the sources)"
        out.append(ConflictNote(
            description=f"'{sides[0]['source_title']}' and '{sides[1]['source_title']}' may contradict each other here{hedge}: {truncate(r.rationale, 220)}",
            relationship_id=r.id, status=r.status, passage_ids=list(r.evidence or []), sides=sides))
    return out


def validate_citations_and_claims(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    hits = {h.passage_id: h for h in _hits(st)}
    drafts = [qa_svc.DraftClaim(d["text"], d["kind"], [tuple(e) for e in d["evidence"]], d.get("memory_refs", [])) for d in st.drafts]
    report = qa_svc.validate_claims(drafts, hits)
    ctx.validation("deterministic_citation_and_support_check", True,
                   {"proposed": len(drafts), "kept": len(report.claims), "dropped": report.dropped, "per_claim": report.checks})
    claims = report.claims
    dropped = len(report.dropped)
    llm = ctx.services.models.llm
    if llm is not None and claims:  # optional second opinion: may only downgrade
        verdicts, usage = qa_svc.llm_check_claims(llm, claims, hits)
        if usage:
            ctx.record_usage(usage)
        keep = []
        for c in claims:
            v = verdicts.get(c.id)
            if v == "unsupported":
                dropped += 1
                ctx.add_to_trace("llm_checker_dropped", c.id)
                continue
            if v == "partial":
                c.status, c.notes = "flagged", c.notes + ["LLM checker: only partially supported"]
            keep.append(c)
        claims = keep
        ctx.validation("llm_second_opinion", True, {"verdicts": verdicts, "note": "advisory; can only downgrade, not a guarantee"})
    conflicts = _conflicts_for(ctx, st, claims)
    cov = qa_svc.question_coverage(st.plan.get("expanded_query") or st.question, claims)
    mode = qa_svc.decide_mode(claims, dropped, conflicts, cov)
    gen_mode = ctx.services.models.mode
    mem_used = []
    disc_ids = {d["memory"] for d in st.plan.get("discrepancies", [])}
    for m in st.memories:
        sids = [t.split(":", 1)[1] for t in m.get("tags", []) if t.startswith("source:")]
        mem_used.append(MemoryUsed(id=m["id"], text=truncate(m["text"], 300), source_ids=sids, role="discrepancy" if m["id"] in disc_ids else "context"))
    warnings = list(st.warnings)
    if mode == "insufficient" and st.raw_hit_count:
        warnings.append("Passages were retrieved but none supported a verifiable claim.")
    ans = AnswerResult(
        mode=mode, answer=qa_svc.render_answer(mode, claims, conflicts, gen_mode), claims=claims, conflicts=conflicts,
        memory_used=mem_used, warnings=warnings, plan={k: v for k, v in st.plan.items() if k != "discrepancies"},
        generation_mode=gen_mode, evidence_considered=len(hits), dropped_claims=dropped,
    )
    st.final = ans.model_dump()
    ctx.validation("answer_mode", True, {"mode": mode, "question_coverage": round(cov, 2), "conflicts": len(conflicts)})
    return StageResult("RETURN_ANSWER")


def return_answer(ctx: RunContext) -> StageResult:
    st: QAState = ctx.state
    if st.final is None:  # branch from RESOLVE_WORKSPACE (empty workspace)
        gen_mode = ctx.services.models.mode
        ans = AnswerResult(mode="insufficient", answer=qa_svc.render_answer("insufficient", [], [], gen_mode), warnings=st.warnings,
                           plan={k: v for k, v in st.plan.items() if k != "discrepancies"}, generation_mode=gen_mode)
        st.final = ans.model_dump()
    if st.conversation_id:
        conv = ctx.session.get(Conversation, st.conversation_id)
        if conv is not None and conv.workspace_id == st.workspace_id:
            ctx.session.add(ChatTurn(conversation_id=conv.id, question=st.question, answer=st.final, execution_id=ctx.execution.id))
    ctx.trace("answer", {"mode": st.final["mode"], "claims": len(st.final["claims"]), "generation_mode": st.final["generation_mode"]})
    return StageResult("RECORD_USEFUL_EXPERIENCE")


def record_useful_experience(ctx: RunContext) -> StageResult:
    """Policy-gated: only cross-source, validated, supported/conflicting synthesis is worth long-term memory."""
    st: QAState = ctx.state
    final = st.final or {}
    claims = final.get("claims", [])
    srcs = sorted({c["source_id"] for cl in claims for c in cl["citations"]})
    reason = None
    if final.get("mode") not in ("supported", "conflicting"):
        reason = f"answer mode '{final.get('mode')}' is not retained"
    elif len(srcs) < 2:
        reason = "single-source answers add nothing beyond the already-retained findings"
    elif st.plan.get("intent") not in ("comparison", "summary"):
        reason = f"intent '{st.plan.get('intent')}' is not a synthesis question"
    elif st.memory_status == "unavailable":
        reason = "memory unavailable"
    if reason:
        st.experience = {"retained": False, "reason": reason}
    else:
        ws = ctx.session.get(Workspace, st.workspace_id)
        titles = sorted({c["source_title"] for cl in claims for c in cl["citations"]})
        body = "; ".join(f"{cl['text']}" for cl in claims[:4])
        content = (f"Cross-source inference (not stated verbatim by any single source). Question researched: '{truncate(st.question, 200)}'. "
                   f"Evidence from {', '.join(repr(t) for t in titles[:4])}: {truncate(body, 500)}")
        try:
            st.experience = {"retained": True, **MemoryService(ctx).retain_experience(
                ws, st.question + "|" + ",".join(srcs), content, "synthesis", srcs,
                context="cross-source synthesis from a validated answer; epistemic=inference", extra_meta={"epistemic": "inference"})}
        except (RetryableError, AppError) as e:  # the answer was already returned; never fail it for a memory error
            st.experience = {"retained": False, "reason": f"memory write failed: {type(e).__name__}"}
    ctx.trace("experience", st.experience)
    ctx.result = {"answer": st.final, "experience": st.experience}
    return StageResult(None)


def build() -> WorkflowDef:
    S = Stage
    stages = [
        S("QUESTION_RECEIVED", question_received, ("CLASSIFY_INTENT",)),
        S("CLASSIFY_INTENT", classify_intent, ("RESOLVE_WORKSPACE",)),
        S("RESOLVE_WORKSPACE", resolve_workspace, ("RECALL_HINDSIGHT_MEMORY", "RETURN_ANSWER")),
        S("RECALL_HINDSIGHT_MEMORY", recall_hindsight_memory, ("RETRIEVE_SOURCE_EVIDENCE",)),
        S("RETRIEVE_SOURCE_EVIDENCE", retrieve_source_evidence, ("MERGE_AND_RANK_EVIDENCE",)),
        S("MERGE_AND_RANK_EVIDENCE", merge_and_rank, ("GENERATE_GROUNDED_ANSWER",)),
        S("GENERATE_GROUNDED_ANSWER", generate_grounded_answer, ("VALIDATE_CITATIONS_AND_CLAIMS",), retries=1),
        S("VALIDATE_CITATIONS_AND_CLAIMS", validate_citations_and_claims, ("RETURN_ANSWER",)),
        S("RETURN_ANSWER", return_answer, ("RECORD_USEFUL_EXPERIENCE",)),
        S("RECORD_USEFUL_EXPERIENCE", record_useful_experience, ()),
    ]
    return WorkflowDef("qa", QAState, {s.name: s for s in stages}, "QUESTION_RECEIVED")
