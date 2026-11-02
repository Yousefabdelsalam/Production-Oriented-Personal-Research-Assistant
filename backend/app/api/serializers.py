"""ORM -> JSON-safe dicts for the API. Source text is only returned from the explicit text/passage endpoints."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    EvidencePassage, ExtractedFinding, MemoryOperation, ResearchRelationship, Source, SourceVersion, SummaryVersion,
    WorkflowExecution, WorkflowStep,
)


def source_dict(s: Source, detail: bool = False, db: Session | None = None) -> dict[str, Any]:
    d = {
        "id": s.id, "workspace_id": s.workspace_id, "kind": s.kind, "title": s.title, "url": s.url, "status": s.status,
        "version": s.current_version, "last_stage": s.last_stage, "error": s.error, "duplicate_of": s.duplicate_of,
        "published_at": s.published_at, "created_at": s.created_at, "updated_at": s.updated_at,
    }
    if detail and db is not None:
        d["versions"] = [
            {"version": v.version, "content_hash": v.content_hash[:16], "size_bytes": v.size_bytes, "mime": v.original_mime,
             "chars": len(v.extracted_text), "meta": v.meta, "created_at": v.created_at}
            for v in db.query(SourceVersion).filter_by(source_id=s.id).order_by(SourceVersion.version).all()
        ]
        d["passages"] = db.query(EvidencePassage).filter_by(source_id=s.id, is_current=True).count()
        d["flagged_passages"] = db.query(EvidencePassage).filter_by(source_id=s.id, is_current=True, suspicious=True).count()
        fs = db.query(ExtractedFinding).filter_by(source_id=s.id, superseded=False).all()
        d["findings"] = {"validated": sum(f.validated for f in fs), "rejected": sum(not f.validated for f in fs),
                         "retained": sum(f.memory_state == "retained" for f in fs)}
        last = (db.query(WorkflowExecution).filter_by(workflow="ingestion", source_id=s.id)
                .order_by(WorkflowExecution.created_at.desc()).first())
        d["latest_execution"] = execution_dict(last) if last else None
    return d


def passage_dict(p: EvidencePassage, source: Source | None = None) -> dict[str, Any]:
    return {
        "id": p.id, "source_id": p.source_id, "source_title": source.title if source else None, "source_kind": source.kind if source else None,
        "workspace_id": p.workspace_id, "ordinal": p.ordinal, "text": p.text, "page_start": p.page_start, "page_end": p.page_end,
        "section": p.section, "char_start": p.char_start, "char_end": p.char_end, "suspicious": p.suspicious, "flags": p.flags,
        "is_current": p.is_current,
    }


def finding_dict(f: ExtractedFinding) -> dict[str, Any]:
    return {
        "id": f.id, "source_id": f.source_id, "passage_id": f.passage_id, "kind": f.kind, "statement": f.statement, "quote": f.quote,
        "epistemic": f.epistemic, "concepts": f.concepts, "page": f.page, "section": f.section, "confidence": f.confidence,
        "extraction_method": f.extraction_method, "validated": f.validated, "validation": f.validation, "memory_state": f.memory_state,
        "superseded": f.superseded,
    }


def relationship_dict(r: ResearchRelationship) -> dict[str, Any]:
    return {
        "id": r.id, "finding_a_id": r.finding_a_id, "finding_b_id": r.finding_b_id, "subject": r.subject, "predicate": r.predicate,
        "object": r.object, "status": r.status, "evidence": r.evidence, "rationale": r.rationale, "detected_by": r.detected_by,
        "active": r.active,
    }


def step_dict(st: WorkflowStep) -> dict[str, Any]:
    return {"id": st.id, "stage": st.stage, "attempt": st.attempt, "status": st.status, "started_at": st.started_at,
            "finished_at": st.finished_at, "duration_ms": st.duration_ms, "detail": st.detail, "error": st.error}


def execution_dict(e: WorkflowExecution, steps: bool = False) -> dict[str, Any]:
    d = {
        "id": e.id, "workflow": e.workflow, "workspace_id": e.workspace_id, "source_id": e.source_id, "correlation_id": e.correlation_id,
        "status": e.status, "current_stage": e.current_stage, "failed_stage": e.failed_stage, "trigger": e.trigger, "error": e.error,
        "resume_count": e.resume_count, "tokens_in": e.tokens_in, "tokens_out": e.tokens_out, "model_calls": e.model_calls,
        "created_at": e.created_at, "started_at": e.started_at, "finished_at": e.finished_at, "duration_ms": e.duration_ms,
    }
    if steps:
        d["steps"] = [step_dict(s) for s in e.steps]
        d["result"] = e.result
    return d


def memory_op_dict(m: MemoryOperation) -> dict[str, Any]:
    return {"id": m.id, "execution_id": m.execution_id, "source_id": m.source_id, "op": m.op, "bank_id": m.bank_id, "status": m.status,
            "request": m.request, "response": m.response, "error": m.error, "duration_ms": m.duration_ms, "created_at": m.created_at}


def summary_dict(v: SummaryVersion, markdown: bool = True) -> dict[str, Any]:
    d = {"id": v.id, "version": v.version, "kind": v.kind, "parent_version": v.parent_version, "change": v.change,
         "source_ids": v.source_ids, "execution_id": v.execution_id, "created_at": v.created_at, "content": v.content}
    if markdown:
        d["markdown"] = v.markdown
    return d
