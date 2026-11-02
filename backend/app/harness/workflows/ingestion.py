"""Workflow A — source ingestion.

RECEIVED → VALIDATE_INPUT → REGISTER_SOURCE → EXTRACT_CONTENT → NORMALIZE_CONTENT → DEDUPLICATE
  → (CHUNK_AND_INDEX → EXTRACT_RESEARCH_FINDINGS → VALIDATE_FINDINGS → RETAIN_USEFUL_MEMORY → UPDATE_KNOWLEDGE_INDEX)
  → MARK_COMPLETED
DEDUPLICATE may jump straight to MARK_COMPLETED (unchanged content / duplicate of another source).

Inputs: IngestionState (source id, kind, stored original / url). Outputs: SourceVersion, EvidencePassages (+FTS),
ExtractedFindings, Hindsight memory (ledgered), follow-up executions. Failure: execution `failed` at the stage,
source `failed`/`partial`; resume restarts at that stage without redoing earlier stages.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.errors import ExtractionError, InvalidInput, NotFound, RetryableError
from app.harness.engine import RunContext, Stage, StageResult, WorkflowDef
from app.harness.injection import scan_text
from app.models import EvidencePassage, ExtractedFinding, Source, SourceVersion, Workspace, utcnow
from app.services import retrieval
from app.services.chunking import DocBlock, chunk_document, normalize_blocks
from app.services.extract_html import extract_html
from app.services.extract_note import extract_note
from app.services.extract_pdf import ExtractedDoc, extract_pdf, validate_pdf_bytes
from app.services.findings import (
    extract_findings_extractive, extract_findings_llm, validate_finding,
)
from app.services.memory import MemoryService
from app.services.safe_fetch import default_resolver, fetch_url, validate_url
from app.services.sources import safe_data_path, store_original
from app.services.textutil import sha256_text, norm_for_match

log = logging.getLogger("ingestion")


class IngestionState(BaseModel):
    kind: str
    workspace_id: str
    source_id: str
    input_path: str | None = None
    input_sha256: str | None = None
    filename: str | None = None
    url: str | None = None
    note_title: str | None = None
    final_url: str | None = None
    raw_path: str | None = None
    raw_mime: str | None = None
    staged_extracted: str | None = None
    staged_normalized: str | None = None
    content_hash: str | None = None
    chars: int = 0
    dedupe_outcome: str | None = None  # new | unchanged | duplicate
    duplicate_of: str | None = None
    version: int | None = None
    version_id: str | None = None
    passage_count: int = 0
    flagged_passages: int = 0
    findings_proposed: int = 0
    findings_validated: int = 0
    findings_rejected: int = 0
    memory: dict[str, Any] = Field(default_factory=dict)
    followups: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------------
def _staging(ctx: RunContext, suffix: str) -> str:
    rel = f"staging/{ctx.execution.id}.{suffix}.json"
    safe_data_path(ctx.services, rel).parent.mkdir(parents=True, exist_ok=True)
    return rel


def _write_json(ctx: RunContext, rel: str, obj: Any) -> None:
    safe_data_path(ctx.services, rel).write_text(json.dumps(obj), encoding="utf-8")


def _read_json(ctx: RunContext, rel: str | None) -> Any:
    if not rel:
        raise ExtractionError("staged extraction missing from state; start a new ingestion", code="staging_lost")
    p = safe_data_path(ctx.services, rel)
    if not p.exists():
        raise ExtractionError("staged extraction file was removed; start a new ingestion", code="staging_lost")
    return json.loads(p.read_text(encoding="utf-8"))


def _source(ctx: RunContext) -> Source:
    src = ctx.session.get(Source, ctx.state.source_id)
    if src is None:
        raise NotFound(f"source {ctx.state.source_id} does not exist")
    if src.workspace_id != ctx.state.workspace_id:
        raise NotFound("source does not belong to this workspace")  # workspace boundary
    return src


# --------------------------------------------------------------------------------------------------
def received(ctx: RunContext) -> StageResult:
    st: IngestionState = ctx.state
    ctx.trace("trigger", {"kind": st.kind, "source_id": st.source_id, "execution": ctx.execution.trigger})
    return StageResult("VALIDATE_INPUT")


def validate_input(ctx: RunContext) -> StageResult:
    st: IngestionState = ctx.state
    if st.kind == "pdf":
        data = safe_data_path(ctx.services, st.input_path or "").read_bytes()
        validate_pdf_bytes(data, ctx.settings.max_upload_mb * 1_000_000)
        ctx.validation("pdf_header_and_size", True, {"bytes": len(data)})
    elif st.kind == "url":
        norm, host, port, ip = validate_url(st.url or "", ctx.services.fetch_resolver or default_resolver)
        ctx.validation("url_ssrf_check", True, {"host": host, "port": port, "resolved_public": True})
    elif st.kind == "note":
        text = safe_data_path(ctx.services, st.input_path or "").read_text(encoding="utf-8")
        extract_note(text, st.note_title, ctx.settings.max_note_chars)
        ctx.validation("note_nonempty_and_size", True, {"chars": len(text)})
    else:
        raise InvalidInput(f"unsupported source kind '{st.kind}'")
    return StageResult("REGISTER_SOURCE")


def register_source(ctx: RunContext) -> StageResult:
    ctx.require("write_sources")
    src = _source(ctx)
    src.status, src.last_stage, src.error = "processing", "REGISTER_SOURCE", None
    ctx.trace("source", {"id": src.id, "kind": src.kind, "previous_version": src.current_version})
    return StageResult("EXTRACT_CONTENT")


def extract_content(ctx: RunContext) -> StageResult:
    ctx.require("read_sources")
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "EXTRACT_CONTENT"
    meta_extra: dict[str, Any] = {}
    if st.kind == "pdf":
        data = safe_data_path(ctx.services, st.input_path or "").read_bytes()
        doc = extract_pdf(data, st.filename or "document.pdf")
        st.raw_path, st.raw_mime = st.input_path, "application/pdf"
    elif st.kind == "url":
        ctx.require("fetch_url")
        res = fetch_url(
            st.url or "", timeout_s=ctx.settings.fetch_timeout_s, max_bytes=ctx.settings.fetch_max_mb * 1_000_000,
            max_redirects=ctx.settings.fetch_max_redirects, resolver=ctx.services.fetch_resolver or default_resolver,
            transport=ctx.services.fetch_transport,
        )
        suffix = ".pdf" if res.content_type == "application/pdf" else ".html" if "html" in res.content_type else ".txt"
        st.raw_path, _ = store_original(ctx.services, res.body, suffix)
        st.raw_mime, st.final_url = res.content_type, res.final_url
        ctx.trace("fetch", {"final_url": res.final_url, "status": res.status, "content_type": res.content_type,
                            "bytes": len(res.body), "redirects": res.redirects})
        if res.content_type == "application/pdf":
            validate_pdf_bytes(res.body, ctx.settings.max_upload_mb * 1_000_000)
            doc = extract_pdf(res.body, st.url or "document.pdf")
        elif "html" in res.content_type:
            doc = extract_html(res.body.decode("utf-8", errors="replace"), res.final_url)
        else:
            doc = extract_note(res.body.decode("utf-8", errors="replace"), None, ctx.settings.max_note_chars)
        meta_extra["fetched_url"] = res.final_url
    else:
        text = safe_data_path(ctx.services, st.input_path or "").read_text(encoding="utf-8")
        doc = extract_note(text, st.note_title, ctx.settings.max_note_chars)
        st.raw_path, st.raw_mime = st.input_path, "text/markdown"
        meta_extra["raw_sha256"] = st.input_sha256
    rel = _staging(ctx, "extracted")
    _write_json(ctx, rel, {
        "title": st.note_title if (st.kind == "note" and st.note_title) else doc.title,
        "blocks": [{"text": b.text, "page": b.page, "section": b.section} for b in doc.blocks],
        "meta": {**doc.meta, **meta_extra}, "quality": doc.quality,
    })
    st.staged_extracted = rel
    ctx.trace("extraction", {"blocks": len(doc.blocks), "quality": doc.quality, "title_len": len(doc.title)})
    ctx.validation("extraction_quality", True, doc.quality)
    return StageResult("NORMALIZE_CONTENT")


def normalize_content(ctx: RunContext) -> StageResult:
    st: IngestionState = ctx.state
    ex = _read_json(ctx, st.staged_extracted)
    doc = normalize_blocks([DocBlock(b["text"], b["page"], b["section"]) for b in ex["blocks"]])
    if len(doc.full_text.strip()) < 20:
        raise ExtractionError("No usable text after normalization.", code="empty_after_normalize")
    rel = _staging(ctx, "normalized")
    _write_json(ctx, rel, {"full_text": doc.full_text, "spans": doc.spans, "title": ex["title"], "meta": ex["meta"], "quality": ex["quality"]})
    st.staged_normalized = rel
    st.content_hash = sha256_text(doc.full_text)
    st.chars = len(doc.full_text)
    ctx.trace("normalized", {"chars": st.chars, "content_hash": st.content_hash[:16]})
    return StageResult("DEDUPLICATE")


def deduplicate(ctx: RunContext) -> StageResult:
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "DEDUPLICATE"
    pending_memory = (
        ctx.session.query(ExtractedFinding)
        .filter_by(source_id=src.id, validated=True, superseded=False, memory_state="pending").count()
        if src.current_version > 0 else 0
    )
    if src.current_version > 0 and src.content_hash == st.content_hash and not pending_memory:
        st.dedupe_outcome = "unchanged"
        ctx.validation("content_dedupe", True, {"outcome": "unchanged", "reason": "identical normalized text already indexed for this source"})
        return StageResult("MARK_COMPLETED")
    other = (
        ctx.session.query(Source)
        .filter(Source.workspace_id == src.workspace_id, Source.id != src.id, Source.content_hash == st.content_hash,
                Source.status == "completed", Source.duplicate_of.is_(None))
        .first()
    )
    if other is not None:
        st.dedupe_outcome, st.duplicate_of = "duplicate", other.id
        ctx.validation("content_dedupe", True, {"outcome": "duplicate", "duplicate_of": other.id})
        return StageResult("MARK_COMPLETED")
    st.dedupe_outcome = "new"
    ctx.validation("content_dedupe", True, {"outcome": "new"})
    return StageResult("CHUNK_AND_INDEX")


def chunk_and_index(ctx: RunContext) -> StageResult:
    ctx.require("write_findings")
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "CHUNK_AND_INDEX"
    nd = _read_json(ctx, st.staged_normalized)
    from app.services.chunking import NormalizedDoc

    doc = NormalizedDoc(full_text=nd["full_text"], spans=[tuple(s) for s in nd["spans"]])
    drafts = chunk_document(doc)
    if not drafts:
        raise ExtractionError("Document produced no passages.", code="no_passages")

    # Idempotent for retries: if this exact content hash was already indexed for this source, reuse it.
    existing = ctx.session.query(SourceVersion).filter_by(source_id=src.id, content_hash=st.content_hash).first()
    if existing is not None and existing.version == src.current_version:
        st.version, st.version_id = existing.version, existing.id
        st.passage_count = ctx.session.query(EvidencePassage).filter_by(source_version_id=existing.id, is_current=True).count()
        ctx.trace("reused_version", {"version": existing.version})
        return StageResult("EXTRACT_RESEARCH_FINDINGS")

    # supersede the previous version: its passages leave the index; its findings are marked superseded
    if src.current_version > 0:
        old_ids = [p.id for p in ctx.session.query(EvidencePassage).filter_by(source_id=src.id, is_current=True).all()]
        retrieval.remove_from_index(ctx.session, old_ids)
        ctx.session.query(EvidencePassage).filter_by(source_id=src.id, is_current=True).update({"is_current": False})
        ctx.session.query(ExtractedFinding).filter_by(source_id=src.id, superseded=False).update({"superseded": True})
        ctx.trace("superseded_previous_version", {"version": src.current_version, "passages_removed": len(old_ids)})

    version = src.current_version + 1
    meta = {**nd["meta"], "extraction_quality": nd["quality"]}
    raw_path = safe_data_path(ctx.services, st.raw_path).resolve() if st.raw_path else None
    sv = SourceVersion(
        source_id=src.id, version=version, content_hash=st.content_hash, original_path=st.raw_path, original_mime=st.raw_mime,
        size_bytes=raw_path.stat().st_size if raw_path and raw_path.exists() else 0, extracted_text=doc.full_text,
        meta=meta, quality=nd["quality"],
    )
    ctx.session.add(sv)
    ctx.session.flush()
    passages: list[EvidencePassage] = []
    flagged = 0
    for i, d in enumerate(drafts):
        flags = scan_text(d.text)
        flagged += bool(flags)
        passages.append(EvidencePassage(
            workspace_id=src.workspace_id, source_id=src.id, source_version_id=sv.id, ordinal=i, text=d.text,
            text_hash=sha256_text(norm_for_match(d.text)), page_start=d.page_start, page_end=d.page_end, section=d.section,
            char_start=d.char_start, char_end=d.char_end, suspicious=bool(flags), flags=flags, is_current=True,
        ))
    ctx.session.add_all(passages)
    ctx.session.flush()
    retrieval.index_passages(ctx.session, passages)
    embedded = retrieval.embed_passages(ctx.session, ctx.services.models.embedder, passages)  # RetryableError => stage retry
    src.current_version, src.content_hash = version, st.content_hash
    src.title = ((st.note_title or src.title or nd["title"]) if src.kind == "note" else nd["title"])[:500]
    src.published_at = nd["meta"].get("published_at")
    st.version, st.version_id, st.passage_count, st.flagged_passages = version, sv.id, len(passages), flagged
    if flagged:
        st.warnings.append(f"{flagged} passage(s) contain instruction-like text; treated strictly as data.")
    ctx.trace("indexed", {"version": version, "passages": len(passages), "flagged": flagged, "embedded": embedded,
                          "pages": sorted({p.page_start for p in passages if p.page_start})[:50]})
    return StageResult("EXTRACT_RESEARCH_FINDINGS")


def extract_research_findings(ctx: RunContext) -> StageResult:
    ctx.require("write_findings")
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "EXTRACT_RESEARCH_FINDINGS"
    passages = (
        ctx.session.query(EvidencePassage).filter_by(source_version_id=st.version_id, is_current=True).order_by(EvidencePassage.ordinal).all()
    )
    is_note = src.kind == "note"
    llm = ctx.services.models.llm
    if llm is not None:
        drafts, usages = extract_findings_llm(llm, passages, src.title, is_note=is_note)
        for u in usages:
            ctx.record_usage(u)
        method = "llm"
    else:
        drafts, method = extract_findings_extractive(passages, is_note=is_note), "extractive"
    # idempotent: replace any findings from an earlier attempt for this version
    ctx.session.query(ExtractedFinding).filter_by(source_version_id=st.version_id).delete()
    by_id = {p.id: p for p in passages}
    for d in drafts:
        p = by_id.get(d.passage_id)
        ctx.session.add(ExtractedFinding(
            workspace_id=src.workspace_id, source_id=src.id, source_version_id=st.version_id, passage_id=d.passage_id,
            kind=d.kind, statement=d.statement, quote=d.quote, epistemic=d.epistemic, concepts=d.concepts,
            page=p.page_start if p else None, section=p.section if p else None, confidence=d.confidence,
            extraction_method=method, validated=False,
        ))
    st.findings_proposed = len(drafts)
    ctx.trace("findings_proposed", {"method": method, "count": len(drafts), "llm_calls": len(ctx.model_usage)})
    return StageResult("VALIDATE_FINDINGS")


def validate_findings(ctx: RunContext) -> StageResult:
    ctx.require("write_findings")
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "VALIDATE_FINDINGS"
    from app.services.findings import FindingDraft

    rows = ctx.session.query(ExtractedFinding).filter_by(source_version_id=st.version_id).all()
    passages = {p.id: p for p in ctx.session.query(EvidencePassage).filter_by(source_version_id=st.version_id).all()}
    seen: set[str] = set()
    ok = bad = 0
    rejections: list[dict[str, Any]] = []
    for f in rows:
        d = FindingDraft(passage_id=f.passage_id, kind=f.kind, statement=f.statement, quote=f.quote, epistemic=f.epistemic,
                         concepts=f.concepts, confidence=f.confidence)
        v = validate_finding(d, passages.get(f.passage_id), is_note=(src.kind == "note"))
        key = sha256_text(norm_for_match(f.statement))
        if v.ok and key in seen:
            v.ok, v.reasons = False, ["duplicate statement within source"]
        f.validated, f.validation = v.ok, {"ok": v.ok, "reasons": v.reasons, "support": v.support}
        if v.ok:
            seen.add(key)
            ok += 1
        else:
            bad += 1
            rejections.append({"finding": f.id, "reasons": v.reasons})
    st.findings_validated, st.findings_rejected = ok, bad
    ctx.validation("findings_schema_quote_support", True, {"validated": ok, "rejected": bad})
    ctx.trace("rejections", rejections[:25])
    if ok == 0:
        st.warnings.append("No findings were validated for this source; evidence is searchable but no memory was created.")
    return StageResult("RETAIN_USEFUL_MEMORY")


def retain_useful_memory(ctx: RunContext) -> StageResult:
    ctx.require("write_memory")
    st: IngestionState = ctx.state
    src = _source(ctx)
    ws = ctx.session.get(Workspace, src.workspace_id)
    findings = ctx.session.query(ExtractedFinding).filter_by(source_version_id=st.version_id, validated=True, superseded=False).all()
    svc = MemoryService(ctx)
    selected, rejected = svc.select_for_retention(findings)
    ctx.trace("retention_policy", {"candidates": len(findings), "selected": len(selected), "rejected": rejected})
    try:
        out = svc.retain_source(ws, src, st.version or 1, selected)
    except RetryableError:
        if ctx.settings.hindsight_required:
            raise
        st.warnings.append("Hindsight unavailable; memory retention deferred (HINDSIGHT_REQUIRED=false).")
        ctx.trace("memory", {"deferred": True})
        return StageResult("UPDATE_KNOWLEDGE_INDEX")
    # DB writes only after the external call + ledger entry (see engine docstring on SQLite locking)
    sel_ids = {f.id for f in selected}
    for f in findings:
        f.memory_state = "retained" if f.id in sel_ids else "skipped"
        if f.id in rejected:
            f.validation = {**f.validation, "retention_rejected": rejected[f.id]}
    src.last_stage = "RETAIN_USEFUL_MEMORY"
    st.memory = out
    ctx.trace("memory", out)
    return StageResult("UPDATE_KNOWLEDGE_INDEX")


def update_knowledge_index(ctx: RunContext) -> StageResult:
    ctx.require("queue_followups")
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.last_stage = "UPDATE_KNOWLEDGE_INDEX"
    ws = ctx.session.get(Workspace, src.workspace_id)
    ws.updated_at = utcnow()
    runner = ctx.services.runner
    if ctx.settings.auto_synthesize and st.findings_validated and "synthesis" in runner.defs:
        from app.harness.workflows.synthesis import SynthesisState

        eid = runner.create(
            "synthesis", state=SynthesisState(workspace_id=ws.id, trigger_source_id=src.id), workspace_id=ws.id, source_id=src.id,
            idempotency_key=f"synth:{ws.id}:{src.id}:v{st.version}", trigger={"type": "after_ingestion", "source_id": src.id},
            correlation=ctx.correlation_id,
        )
        st.followups.append(eid)
    ctx.trace("followups", st.followups)
    return StageResult("MARK_COMPLETED")


def mark_completed(ctx: RunContext) -> StageResult:
    st: IngestionState = ctx.state
    src = _source(ctx)
    src.status, src.last_stage, src.error = "completed", "MARK_COMPLETED", None
    if st.dedupe_outcome == "duplicate":
        src.duplicate_of = st.duplicate_of
    elif st.dedupe_outcome == "new":
        src.duplicate_of = None
    for rel in (st.staged_extracted, st.staged_normalized):
        if rel:
            try:
                safe_data_path(ctx.services, rel).unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove staging file", extra={"path": rel})
    ctx.result = {
        "source_id": src.id, "version": st.version, "outcome": st.dedupe_outcome, "duplicate_of": st.duplicate_of,
        "passages": st.passage_count, "findings_validated": st.findings_validated, "findings_rejected": st.findings_rejected,
        "memory": st.memory, "warnings": st.warnings,
    }
    return StageResult(None)


def on_failure(ctx: RunContext, err: Exception) -> None:
    src = ctx.session.get(Source, ctx.state.source_id)
    if src is None:
        return
    has_index = ctx.session.query(EvidencePassage).filter_by(source_id=src.id, is_current=True).first() is not None
    # evidence searchable but a later stage failed => 'partial' (visible + recoverable); otherwise 'failed'
    src.status = "partial" if has_index else "failed"
    src.error = f"{ctx.execution.failed_stage or ctx.stage}: {type(err).__name__}: {str(err)[:600]}"
    src.last_stage = ctx.execution.failed_stage or ctx.stage


def build() -> WorkflowDef:
    S = Stage
    stages = [
        S("RECEIVED", received, ("VALIDATE_INPUT",)),
        S("VALIDATE_INPUT", validate_input, ("REGISTER_SOURCE",), retries=2),  # DNS hiccups are retryable
        S("REGISTER_SOURCE", register_source, ("EXTRACT_CONTENT",)),
        S("EXTRACT_CONTENT", extract_content, ("NORMALIZE_CONTENT",), retries=2),
        S("NORMALIZE_CONTENT", normalize_content, ("DEDUPLICATE",)),
        S("DEDUPLICATE", deduplicate, ("CHUNK_AND_INDEX", "MARK_COMPLETED")),
        S("CHUNK_AND_INDEX", chunk_and_index, ("EXTRACT_RESEARCH_FINDINGS",), retries=2),
        S("EXTRACT_RESEARCH_FINDINGS", extract_research_findings, ("VALIDATE_FINDINGS",), retries=1),
        S("VALIDATE_FINDINGS", validate_findings, ("RETAIN_USEFUL_MEMORY",)),
        S("RETAIN_USEFUL_MEMORY", retain_useful_memory, ("UPDATE_KNOWLEDGE_INDEX",), retries=2),
        S("UPDATE_KNOWLEDGE_INDEX", update_knowledge_index, ("MARK_COMPLETED",)),
        S("MARK_COMPLETED", mark_completed, ()),
    ]
    return WorkflowDef("ingestion", IngestionState, {s.name: s for s in stages}, "RECEIVED", on_failure=on_failure)
