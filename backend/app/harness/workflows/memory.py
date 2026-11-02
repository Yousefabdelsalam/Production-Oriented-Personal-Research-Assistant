"""Workflow E — Hindsight memory retention backfill and consolidation.

ENSURE_BANK → SELECT_UNRETAINED → RETAIN_PENDING → CONSOLIDATE → DONE

* Backfills validated findings whose retention was deferred (Hindsight outage with HINDSIGHT_REQUIRED=false).
* Creates / refreshes a Hindsight *mental model* ("workspace-overview") that consolidates the bank's accumulated knowledge.
  That output is AI-generated memory: the UI shows it as such and it is never used as citable evidence.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from app.errors import AppError, RetryableError
from app.harness.engine import RunContext, Stage, StageResult, WorkflowDef
from app.models import ExtractedFinding, Source, Workspace
from app.services.hindsight_gateway import HindsightError, HindsightUnavailable
from app.services.memory import MemoryService

log = logging.getLogger("memory-wf")
MODEL_ID = "workspace-overview"
MODEL_QUERY = ("What is currently known in this research workspace? Summarise the key findings, definitions and methods per source, "
               "and clearly state where sources disagree or evidence is uncertain. Keep source attribution.")


class MemoryState(BaseModel):
    workspace_id: str
    reason: str = ""
    pending_sources: list[str] = Field(default_factory=list)
    retained: dict[str, Any] = Field(default_factory=dict)
    consolidation: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def ensure_bank(ctx: RunContext) -> StageResult:
    ctx.require("write_memory")
    ws = ctx.session.get(Workspace, ctx.state.workspace_id)
    MemoryService(ctx).ensure_bank(ws)
    ctx.trace("bank", {"id": ws.bank_id})
    return StageResult("SELECT_UNRETAINED")


def select_unretained(ctx: RunContext) -> StageResult:
    st: MemoryState = ctx.state
    rows = (ctx.session.query(ExtractedFinding.source_id)
            .filter_by(workspace_id=st.workspace_id, validated=True, superseded=False, memory_state="pending").distinct().all())
    st.pending_sources = [r[0] for r in rows]
    ctx.trace("pending_sources", st.pending_sources)
    return StageResult("RETAIN_PENDING")


def retain_pending(ctx: RunContext) -> StageResult:
    ctx.require("write_memory")
    st: MemoryState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    svc = MemoryService(ctx)
    totals = {"sources": 0, "retained": 0}
    for sid in st.pending_sources:
        src = ctx.session.get(Source, sid)
        findings = ctx.session.query(ExtractedFinding).filter_by(source_id=sid, validated=True, superseded=False, memory_state="pending").all()
        selected, rejected = svc.select_for_retention(findings)
        out = svc.retain_source(ws, src, src.current_version, selected)  # RetryableError => stage retry; ledger prevents repeats
        sel = {f.id for f in selected}
        for f in findings:
            f.memory_state = "retained" if f.id in sel else "skipped"
            if f.id in rejected:
                f.validation = {**f.validation, "retention_rejected": rejected[f.id]}
        totals["sources"] += 1
        totals["retained"] += out.get("retained", 0)
    st.retained = totals
    ctx.trace("retained", totals)
    return StageResult("CONSOLIDATE")


def consolidate(ctx: RunContext) -> StageResult:
    ctx.require("consolidate_memory")
    st: MemoryState = ctx.state
    ws = ctx.session.get(Workspace, st.workspace_id)
    t0 = time.perf_counter()
    try:
        out = ctx.services.gateway.consolidate(ws.bank_id, MODEL_ID, f"{ws.name} — consolidated view", MODEL_QUERY)
    except HindsightUnavailable as e:
        raise RetryableError(f"Hindsight unavailable during consolidation: {e}", code="hindsight_unavailable") from e
    except HindsightError as e:
        raise AppError(f"Hindsight rejected consolidation: {e}", code="hindsight_rejected") from e
    ms = int((time.perf_counter() - t0) * 1000)
    ctx.services.ledger_record(key=None, execution_id=ctx.execution.id, workspace_id=ws.id, op="consolidate", bank_id=ws.bank_id,
                               status="done", request={"mental_model": MODEL_ID, "reason": st.reason}, response=out, duration_ms=ms)
    st.consolidation = out
    ctx.trace("consolidation", {**out, "note": "asynchronous on the Hindsight side; content appears when its refresh finishes"})
    return StageResult("DONE")


def done(ctx: RunContext) -> StageResult:
    st: MemoryState = ctx.state
    ctx.result = {"retained": st.retained, "consolidation": st.consolidation, "warnings": st.warnings}
    return StageResult(None)


def build() -> WorkflowDef:
    S = Stage
    stages = [
        S("ENSURE_BANK", ensure_bank, ("SELECT_UNRETAINED",), retries=2),
        S("SELECT_UNRETAINED", select_unretained, ("RETAIN_PENDING",)),
        S("RETAIN_PENDING", retain_pending, ("CONSOLIDATE",), retries=2),
        S("CONSOLIDATE", consolidate, ("DONE",), retries=2),
        S("DONE", done, ()),
    ]
    return WorkflowDef("memory", MemoryState, {s.name: s for s in stages}, "ENSURE_BANK")
