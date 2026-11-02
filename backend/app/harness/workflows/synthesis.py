"""Workflow C — cross-source synthesis.

LOAD_FINDINGS → SELECT_CANDIDATE_PAIRS → PROPOSE_RELATIONSHIPS → VALIDATE_RELATIONSHIPS → PERSIST_RELATIONSHIPS
  → RETAIN_RELATIONSHIP_MEMORY → COMPLETE

Maker (heuristic or LLM) proposes; deterministic validator decides what is stored and with which status.
Heuristic proposals are always `hypothesis`. Only validated findings from different sources can be related.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.errors import AppError, RetryableError
from app.harness.engine import RunContext, Stage, StageResult, WorkflowDef
from app.models import ExtractedFinding, ResearchRelationship, Source, Workspace
from app.services import relations as rel
from app.services.memory import MemoryService
from app.services.textutil import truncate

log = logging.getLogger("synthesis")


class SynthesisState(BaseModel):
    workspace_id: str
    trigger_source_id: str | None = None
    findings_loaded: int = 0
    pairs: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    accepted: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    relationship_ids: list[str] = Field(default_factory=list)
    memory: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _findings(ctx: RunContext) -> list[ExtractedFinding]:
    st: SynthesisState = ctx.state
    return (ctx.session.query(ExtractedFinding)
            .filter(ExtractedFinding.workspace_id == st.workspace_id, ExtractedFinding.validated.is_(True), ExtractedFinding.superseded.is_(False))
            .order_by(ExtractedFinding.created_at).all())


def load_findings(ctx: RunContext) -> StageResult:
    ctx.require("read_sources")
    st: SynthesisState = ctx.state
    fs = _findings(ctx)
    st.findings_loaded = len(fs)
    ctx.trace("loaded", {"findings": len(fs), "sources": len({f.source_id for f in fs}), "incremental_for": st.trigger_source_id})
    return StageResult("SELECT_CANDIDATE_PAIRS")


def select_candidate_pairs(ctx: RunContext) -> StageResult:
    st: SynthesisState = ctx.state
    fs = _findings(ctx)
    pairs = rel.candidate_pairs(fs, st.trigger_source_id)
    st.pairs = [{"a": p.a.id, "b": p.b.id, "shared": p.shared, "overlap": round(p.overlap, 3), "score": round(p.score, 3)} for p in pairs]
    ctx.trace("candidate_pairs", {"count": len(pairs), "top": st.pairs[:10],
                                  "why": "pairs from different sources sharing ≥3 topic terms (or ≥2 plus a shared figure); everything else is never related"})
    return StageResult("PROPOSE_RELATIONSHIPS")


def propose_relationships(ctx: RunContext) -> StageResult:
    st: SynthesisState = ctx.state
    fs = {f.id: f for f in _findings(ctx)}
    pairs = [rel.Pair(fs[p["a"]], fs[p["b"]], p["shared"], p["overlap"], p["score"]) for p in st.pairs if p["a"] in fs and p["b"] in fs]
    llm = ctx.services.models.llm
    if llm is not None and pairs:
        titles = {s.id: s.title for s in ctx.session.query(Source).filter_by(workspace_id=st.workspace_id).all()}
        proposals, usages = rel.propose_llm(llm, pairs, titles)
        for u in usages:
            ctx.record_usage(u)
        method = "llm"
    else:
        proposals, method = rel.propose_heuristic(list(fs.values()), pairs), "heuristic"
    st.proposals = [p.__dict__ for p in proposals]
    ctx.trace("proposals", {"method": method, "count": len(proposals)})
    return StageResult("VALIDATE_RELATIONSHIPS")


def validate_relationships(ctx: RunContext) -> StageResult:
    st: SynthesisState = ctx.state
    fs = {f.id: f for f in _findings(ctx)}
    st.accepted, st.rejected = [], []
    for raw in st.proposals:
        p = rel.Proposal(**raw)
        v = rel.validate_proposal(p, fs)
        if v.ok:
            st.accepted.append({**raw, "status": v.status})
        else:
            st.rejected.append({**raw, "reasons": v.reasons})
    ctx.validation("relationship_evidence_and_overlap", True, {"accepted": len(st.accepted), "rejected": len(st.rejected)})
    ctx.trace("rejected", [{"a": r["a_id"], "b": r["b_id"], "predicate": r["predicate"], "reasons": r["reasons"]} for r in st.rejected[:20]])
    return StageResult("PERSIST_RELATIONSHIPS")


def persist_relationships(ctx: RunContext) -> StageResult:
    ctx.require("write_relationships")
    st: SynthesisState = ctx.state
    fs = {f.id: f for f in _findings(ctx)}
    for raw in st.accepted:
        a, b = fs[raw["a_id"]], fs[raw["b_id"]]
        row = (ctx.session.query(ResearchRelationship)
               .filter_by(workspace_id=st.workspace_id, finding_a_id=a.id, finding_b_id=b.id, predicate=raw["predicate"]).first())
        if row is None:
            row = ResearchRelationship(workspace_id=st.workspace_id, finding_a_id=a.id, finding_b_id=b.id, predicate=raw["predicate"],
                                       subject=truncate(a.statement, 190), object=truncate(b.statement, 190))
            ctx.session.add(row)
            ctx.session.flush()
            st.created += 1
        else:
            st.updated += 1
        # never downgrade a user-confirmed/rejected decision
        if row.detected_by != "user":
            row.status, row.detected_by = raw["status"], raw["detected_by"]
            row.rationale = raw["rationale"]
        row.evidence = [a.passage_id, b.passage_id]
        row.active = True
        st.relationship_ids.append(row.id)
    # relationships whose findings were superseded are no longer active
    live = set(fs)
    stale = ctx.session.query(ResearchRelationship).filter(ResearchRelationship.workspace_id == st.workspace_id, ResearchRelationship.active.is_(True)).all()
    for r in stale:
        if r.finding_a_id not in live or r.finding_b_id not in live:
            r.active = False
            st.deactivated += 1
    ctx.trace("persisted", {"created": st.created, "updated": st.updated, "deactivated": st.deactivated})
    return StageResult("RETAIN_RELATIONSHIP_MEMORY")


def retain_relationship_memory(ctx: RunContext) -> StageResult:
    """Only contradictions (flagged as such, hypothesis or not) and `supported` links are worth long-term memory."""
    ctx.require("write_memory")
    st: SynthesisState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    rows = ctx.session.query(ResearchRelationship).filter(ResearchRelationship.id.in_(st.relationship_ids or ["-"])).all()
    fs = {f.id: f for f in ctx.session.query(ExtractedFinding).filter(ExtractedFinding.id.in_([x for r in rows for x in (r.finding_a_id, r.finding_b_id)] or ["-"])).all()}
    titles = {s.id: s.title for s in ctx.session.query(Source).filter_by(workspace_id=st.workspace_id).all()}
    svc = MemoryService(ctx)
    retained = skipped = 0
    for r in rows:
        if not (r.predicate == "contradicts" or r.status == "supported") or r.status == "rejected":
            skipped += 1
            continue
        a, b = fs[r.finding_a_id], fs[r.finding_b_id]
        label = "Relationship (supported)" if r.status == "supported" else "Unverified hypothesis — possible relationship"
        content = (f"{label}: '{titles.get(a.source_id, '')}' and '{titles.get(b.source_id, '')}' may {r.predicate.replace('_', ' ')} each other. "
                   f"{truncate(a.statement, 220)} || {truncate(b.statement, 220)}. This is an inference, not a statement made by either source.")
        try:
            out = svc.retain_experience(ws, f"{r.id}|{r.status}|{r.finding_a_id}|{r.finding_b_id}", content, "relationship", [a.source_id, b.source_id],
                                        context=f"cross-source {r.predicate}; status={r.status}; origin=inference",
                                        extra_meta={"relationship_id": r.id, "status": r.status, "epistemic": "inference"})
            retained += out.get("retained", 0)
        except RetryableError:
            if ctx.settings.hindsight_required:
                raise
            st.warnings.append("Hindsight unavailable; relationship memory deferred.")
            break
    st.memory = {"retained": retained, "skipped": skipped}
    ctx.trace("memory", st.memory)
    return StageResult("COMPLETE")


def complete(ctx: RunContext) -> StageResult:
    ctx.require("queue_followups")
    st: SynthesisState = ctx.state
    followups: list[str] = []
    runner = ctx.services.runner
    if ctx.settings.auto_update_summary and "summary" in runner.defs:
        from app.harness.workflows.summary import SummaryState

        # still a proposal: it only becomes a new version if it validates AND changes something
        followups.append(runner.create("summary", state=SummaryState(workspace_id=st.workspace_id), workspace_id=st.workspace_id,
                                       idempotency_key=f"summary:after:{ctx.execution.id}", trigger={"type": "after_synthesis"},
                                       correlation=ctx.correlation_id))
    ctx.result = {"created": st.created, "updated": st.updated, "deactivated": st.deactivated, "rejected": len(st.rejected),
                  "memory": st.memory, "warnings": st.warnings, "followups": followups}
    return StageResult(None)


def build() -> WorkflowDef:
    S = Stage
    stages = [
        S("LOAD_FINDINGS", load_findings, ("SELECT_CANDIDATE_PAIRS",)),
        S("SELECT_CANDIDATE_PAIRS", select_candidate_pairs, ("PROPOSE_RELATIONSHIPS",)),
        S("PROPOSE_RELATIONSHIPS", propose_relationships, ("VALIDATE_RELATIONSHIPS",), retries=1),
        S("VALIDATE_RELATIONSHIPS", validate_relationships, ("PERSIST_RELATIONSHIPS",)),
        S("PERSIST_RELATIONSHIPS", persist_relationships, ("RETAIN_RELATIONSHIP_MEMORY",)),
        S("RETAIN_RELATIONSHIP_MEMORY", retain_relationship_memory, ("COMPLETE",), retries=2),
        S("COMPLETE", complete, ()),
    ]
    return WorkflowDef("synthesis", SynthesisState, {s.name: s for s in stages}, "LOAD_FINDINGS")
