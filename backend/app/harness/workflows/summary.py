"""Workflow D — living summary generation, update and rollback.

LOAD_CURRENT → GATHER_EVIDENCE → PROPOSE_SUMMARY → VALIDATE_PROPOSAL → COMPARE_WITH_CURRENT
   → PERSIST_VERSION → QUEUE_CONSOLIDATION → DONE            (meaningful change)
   → DONE                                                     (no meaningful change: NO new version is created)

The last valid version is never touched by a failed run: only PERSIST_VERSION (the one stage with the
`modify_summary` capability) writes, and only after VALIDATE_PROPOSAL passed. Rollback = a NEW version copied
from an old one (history stays append-only).
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.errors import AppError, NotFound, ValidationFailed
from app.harness.engine import RunContext, Stage, StageResult, WorkflowDef
from app.harness.injection import scan_text
from app.models import Source, SummaryVersion, Workspace
from app.services import summary as sm
from app.services.llm import with_rules, wrap_untrusted

log = logging.getLogger("summary")


class SummaryState(BaseModel):
    workspace_id: str
    action: str = "update"  # update | rollback
    rollback_to: int | None = None
    current_version: int = 0
    previous: dict[str, Any] | None = None
    new_source_ids: list[str] = Field(default_factory=list)
    proposal: dict[str, Any] | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    removal_reasons: dict[str, str] = Field(default_factory=dict)
    diff: dict[str, Any] = Field(default_factory=dict)
    new_version: int | None = None
    outcome: str = ""  # created | no_change
    followups: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _current(ctx: RunContext, ws_id: str) -> SummaryVersion | None:
    ws = ctx.session.get(Workspace, ws_id)
    if ws is None:
        raise NotFound(f"workspace {ws_id} not found")
    if ws.current_summary_version <= 0:
        return None
    return ctx.session.query(SummaryVersion).filter_by(workspace_id=ws_id, version=ws.current_summary_version).first()


def load_current(ctx: RunContext) -> StageResult:
    ctx.require("read_sources")
    st: SummaryState = ctx.state
    cur = _current(ctx, st.workspace_id)
    if cur is not None:
        st.current_version = cur.version
        st.previous = cur.content
    ctx.trace("current", {"version": st.current_version, "items": len(sm.parse_content(cur.content).all_items()) if cur else 0})
    if st.action == "rollback":
        if not st.rollback_to or st.rollback_to == st.current_version:
            raise ValidationFailed("rollback target must be an existing, different version", code="bad_rollback")
        if ctx.session.query(SummaryVersion).filter_by(workspace_id=st.workspace_id, version=st.rollback_to).first() is None:
            raise NotFound(f"summary version {st.rollback_to} does not exist")
    return StageResult("GATHER_EVIDENCE")


def gather_evidence(ctx: RunContext) -> StageResult:
    st: SummaryState = ctx.state
    prev = sm.parse_content(st.previous) if st.previous else None
    known = set(prev.source_ids) if prev else set()
    srcs = ctx.session.query(Source).filter(Source.workspace_id == st.workspace_id, Source.status == "completed", Source.duplicate_of.is_(None),
                                            Source.current_version > 0).all()
    st.new_source_ids = [s.id for s in srcs if s.id not in known]
    items, stats = sm.build_items(ctx.session, st.workspace_id)
    ctx.trace("gathered", {"findings": stats["findings"], "sources": stats["sources"], "relationships": stats["relationships"],
                           "candidate_items": len(items), "sources_not_in_previous_version": st.new_source_ids})
    return StageResult("PROPOSE_SUMMARY")


def _llm_overview(ctx: RunContext, content: sm.SummaryContent, ws_name: str) -> str | None:
    llm = ctx.services.models.llm
    if llm is None:
        return None

    class _Ov(BaseModel):
        overview: str

    body = "\n".join(wrap_untrusted(f"item {i.key} ({i.section}, {i.origin})", i.text) for i in content.all_items()[:40])
    try:
        out, usage = llm.complete_json(
            purpose="summary_overview",
            system=with_rules("Write a 2-4 sentence neutral overview of this research workspace using ONLY the listed summary items. "
                              "Do not introduce numbers, names or claims that are not in the items. Mention disagreements if any item is a contradiction."),
            user=f"Workspace: {ws_name}\n\n{body}", schema=_Ov, max_tokens=500)
    except AppError as e:
        ctx.add_to_trace("llm_overview_failed", str(e)[:200])
        return None
    ctx.record_usage(usage)
    return out.overview.strip()


def propose_summary(ctx: RunContext) -> StageResult:
    st: SummaryState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    prev = sm.parse_content(st.previous) if st.previous else None
    if st.action == "rollback":
        target = ctx.session.query(SummaryVersion).filter_by(workspace_id=st.workspace_id, version=st.rollback_to).one()
        proposal = sm.parse_content(target.content)
        ctx.trace("proposal", {"method": "rollback", "from_version": target.version, "items": len(proposal.all_items())})
        st.proposal = proposal.model_dump()
        return StageResult("VALIDATE_PROPOSAL")
    items, stats = sm.build_items(ctx.session, st.workspace_id)
    valid_prev = {i.key for i in prev.all_items() if sm.removal_reason(ctx.session, st.workspace_id, i) is None} if prev else set()
    content = sm.assemble(items, stats, prev, valid_prev, ws.name)
    method = "deterministic"
    ov = _llm_overview(ctx, content, ws.name)
    if ov:
        content = content.model_copy(update={"overview": ov, "overview_mode": "llm"})
        method = "deterministic items + llm overview"
    st.proposal = content.model_dump()
    ctx.trace("proposal", {"method": method, "items": len(content.all_items()), "carried_forward": len(valid_prev),
                           "sections": {k: len(v) for k, v in content.sections.items()}})
    return StageResult("VALIDATE_PROPOSAL")


def validate_proposal(ctx: RunContext) -> StageResult:
    st: SummaryState = ctx.state
    content = sm.parse_content(st.proposal)
    prev = sm.parse_content(st.previous) if st.previous else None
    rep = sm.validate_content(ctx.session, st.workspace_id, content, None if st.action == "rollback" else prev,
                              require_current=(st.action != "rollback"))
    if not rep.ok and content.overview_mode == "llm":
        # an LLM overview that fails validation is replaced by the deterministic one; item problems are never papered over
        only_overview = all(p.startswith("overview") for p in rep.problems)
        if only_overview:
            items, stats = sm.build_items(ctx.session, st.workspace_id)
            fallback = sm.assemble(items, stats, prev, {i.key for i in prev.all_items()} if prev else set(), ctx.session.get(Workspace, st.workspace_id).name)
            content = content.model_copy(update={"overview": fallback.overview, "overview_mode": "deterministic"})
            st.proposal = content.model_dump()
            st.warnings.append("LLM overview failed validation (" + "; ".join(rep.problems)[:200] + "); deterministic overview used.")
            rep = sm.validate_content(ctx.session, st.workspace_id, content, prev)
    st.validation = rep.model_dump()
    ctx.validation("summary_schema_citations_support_preservation", rep.ok, rep.model_dump())
    if not rep.ok:
        raise ValidationFailed("Proposed summary failed validation; the last valid version was kept. Problems: " + " | ".join(rep.problems[:6]),
                               code="summary_invalid")
    return StageResult("COMPARE_WITH_CURRENT")


def compare_with_current(ctx: RunContext) -> StageResult:
    st: SummaryState = ctx.state
    new = sm.parse_content(st.proposal)
    old = sm.parse_content(st.previous) if st.previous else None
    reasons: dict[str, str] = {}
    if old:
        for it in old.all_items():
            r = sm.removal_reason(ctx.session, st.workspace_id, it)
            if r:
                reasons[it.key] = r
            elif st.action == "rollback":
                reasons[it.key] = f"rolled back to version {st.rollback_to}"
    st.removal_reasons = reasons
    st.diff = sm.diff_contents(old, new, reasons)
    ctx.trace("diff", {k: (len(v) if isinstance(v, list) else v) for k, v in st.diff.items()})
    if old is None and not new.all_items():
        st.outcome = "no_change"
        ctx.trace("decision", "nothing to summarise yet: no validated findings, no version created")
        return StageResult("DONE")
    if old is not None and not st.diff["meaningful"] and st.action == "update":
        st.outcome = "no_change"
        ctx.trace("decision", "no meaningful change (no items added, removed or changed): keep the current version")
        return StageResult("DONE")
    return StageResult("PERSIST_VERSION")


def persist_version(ctx: RunContext) -> StageResult:
    ctx.require("modify_summary")
    st: SummaryState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    content = sm.parse_content(st.proposal)
    version = ws.current_summary_version + 1
    if st.action == "update":  # stamp version bookkeeping on items
        for it in content.all_items():
            if it.first_version == 0:
                it.first_version = version
            if it.key in {c["key"] for c in st.diff["added"]} | {c["key"] for c in st.diff["changed"]}:
                it.last_changed_version = version
    titles = {s.id: s.title for s in ctx.session.query(Source).filter_by(workspace_id=ws.id).all()}
    kinds = {s.id: s.kind for s in ctx.session.query(Source).filter_by(workspace_id=ws.id).all()}
    md = sm.render_markdown(content, ws.name, version, titles, kinds)
    d = st.diff
    line = (f"Rolled back to version {st.rollback_to}. " if st.action == "rollback" else "") + \
           f"{len(d['added'])} added, {len(d['changed'])} changed, {len(d['removed'])} removed."
    deferred = int(content.stats.get("deferred_items", 0) or 0)
    if deferred and st.action == "update":
        line += f" {deferred} more candidate item(s) were deferred by the per-update cap; run Update again to include them."
    ctx.session.add(SummaryVersion(
        workspace_id=ws.id, version=version, content=content.model_dump(), markdown=md,
        change={"summary": line, "added": d["added"], "removed": d["removed"], "changed": d["changed"], "new_source_ids": st.new_source_ids,
                "rolled_back_to": st.rollback_to, "validation": {"ok": True, "items": st.validation.get("checked_items"),
                                                                 "citations": st.validation.get("checked_citations")}},
        source_ids=content.source_ids, execution_id=ctx.execution.id, parent_version=st.current_version or None,
        kind="rollback" if st.action == "rollback" else "generated",
    ))
    ws.current_summary_version = version
    st.new_version, st.outcome = version, "created"
    ctx.trace("persisted", {"version": version, "change": line})
    return StageResult("QUEUE_CONSOLIDATION")


def queue_consolidation(ctx: RunContext) -> StageResult:
    ctx.require("queue_followups")
    st: SummaryState = ctx.state
    runner = ctx.services.runner
    if "memory" in runner.defs:
        from app.harness.workflows.memory import MemoryState

        eid = runner.create("memory", state=MemoryState(workspace_id=st.workspace_id, reason=f"summary v{st.new_version}"),
                            workspace_id=st.workspace_id, idempotency_key=f"consolidate:{st.workspace_id}:v{st.new_version}",
                            trigger={"type": "after_summary", "version": st.new_version}, correlation=ctx.correlation_id)
        st.followups.append(eid)
    ctx.trace("followups", st.followups)
    return StageResult("DONE")


def done(ctx: RunContext) -> StageResult:
    st: SummaryState = ctx.state
    ctx.result = {"outcome": st.outcome, "version": st.new_version or st.current_version, "created_version": st.new_version,
                  "diff": {k: (len(v) if isinstance(v, list) else v) for k, v in st.diff.items()}, "warnings": st.warnings,
                  "followups": st.followups,
                  "deferred_items": int((sm.parse_content(st.proposal).stats.get("deferred_items", 0) or 0)) if st.proposal else 0}
    return StageResult(None)


def build() -> WorkflowDef:
    S = Stage
    stages = [
        S("LOAD_CURRENT", load_current, ("GATHER_EVIDENCE",)),
        S("GATHER_EVIDENCE", gather_evidence, ("PROPOSE_SUMMARY",)),
        S("PROPOSE_SUMMARY", propose_summary, ("VALIDATE_PROPOSAL",), retries=1),
        S("VALIDATE_PROPOSAL", validate_proposal, ("COMPARE_WITH_CURRENT",)),
        S("COMPARE_WITH_CURRENT", compare_with_current, ("PERSIST_VERSION", "DONE")),
        S("PERSIST_VERSION", persist_version, ("QUEUE_CONSOLIDATION",)),
        S("QUEUE_CONSOLIDATION", queue_consolidation, ("DONE",)),
        S("DONE", done, ()),
    ]
    return WorkflowDef("summary", SummaryState, {s.name: s for s in stages}, "LOAD_CURRENT")
