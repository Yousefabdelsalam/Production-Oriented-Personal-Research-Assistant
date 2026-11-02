"""Source submission: the door of workflow A. Does cheap, synchronous prechecks, preserves originals
(content-addressed), registers source identity idempotently and queues the ingestion execution."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.container import Services
from app.errors import InvalidInput, NotFound
from app.models import Source, SourceVersion, Workspace, WorkflowExecution
from app.services.extract_note import extract_note
from app.services.extract_pdf import validate_pdf_bytes
from app.services.safe_fetch import canonical_url, default_resolver, validate_url
from app.services.textutil import sha256_bytes, sha256_text


@dataclass
class SubmitResult:
    source_id: str
    execution_id: str | None
    duplicate: bool
    state: str  # "queued" | "duplicate_completed" | "duplicate_in_progress" | "duplicate_needs_retry" | "unchanged"
    message: str


def safe_data_path(services: Services, rel: str) -> Path:
    base = Path(services.settings.data_dir).resolve()
    p = (base / rel).resolve()
    if base not in p.parents and p != base:
        raise InvalidInput("path escapes data directory", code="bad_path")
    return p


def store_original(services: Services, data: bytes, suffix: str) -> tuple[str, str]:
    """Content-addressed storage; returns (path relative to data dir, sha256)."""
    sha = sha256_bytes(data)
    rel = f"originals/{sha[:2]}/{sha}{suffix}"
    p = safe_data_path(services, rel)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    return rel, sha


def _latest_execution(s: Session, source_id: str) -> WorkflowExecution | None:
    return (
        s.query(WorkflowExecution).filter_by(workflow="ingestion", source_id=source_id)
        .order_by(WorkflowExecution.created_at.desc()).first()
    )


def _get_or_create_source(s: Session, ws_id: str, kind: str, dedupe_key: str, title: str, url: str | None) -> tuple[Source, bool]:
    existing = s.query(Source).filter_by(workspace_id=ws_id, dedupe_key=dedupe_key).first()
    if existing is not None:
        return existing, False
    src = Source(workspace_id=ws_id, kind=kind, dedupe_key=dedupe_key, title=title[:500], url=url, status="queued")
    try:
        with s.begin_nested():
            s.add(src)
            s.flush()
    except IntegrityError:  # lost a race with a concurrent identical submission
        return s.query(Source).filter_by(workspace_id=ws_id, dedupe_key=dedupe_key).one(), False
    return src, True


def _duplicate_outcome(s: Session, src: Source) -> SubmitResult:
    last = _latest_execution(s, src.id)
    if src.status == "completed":
        return SubmitResult(src.id, last.id if last else None, True, "duplicate_completed",
                            "This source is already in the workspace; nothing was re-processed.")
    if src.status in ("queued", "processing"):
        return SubmitResult(src.id, last.id if last else None, True, "duplicate_in_progress",
                            "This source is already being processed.")
    return SubmitResult(src.id, last.id if last else None, True, "duplicate_needs_retry",
                        f"This source was submitted before and ended as '{src.status}'. Retry it from the source library.")


def _queue_ingestion(services: Services, ws_id: str, src: Source, state: dict[str, Any], key: str, trigger: dict[str, Any]) -> str:
    from app.harness.workflows.ingestion import IngestionState

    return services.runner.create(
        "ingestion", state=IngestionState(workspace_id=ws_id, source_id=src.id, kind=src.kind, **state),
        workspace_id=ws_id, source_id=src.id, idempotency_key=key, trigger=trigger,
    )


def _workspace(s: Session, ws_id: str) -> Workspace:
    ws = s.get(Workspace, ws_id)
    if ws is None:
        raise NotFound(f"workspace {ws_id} not found")
    return ws


def submit_pdf(services: Services, ws_id: str, filename: str, data: bytes) -> SubmitResult:
    validate_pdf_bytes(data, services.settings.max_upload_mb * 1_000_000)
    rel, sha = store_original(services, data, ".pdf")
    with services.session() as s:
        _workspace(s, ws_id)
        src, created = _get_or_create_source(s, ws_id, "pdf", f"pdf:{sha}", filename.rsplit(".", 1)[0] or "PDF", None)
        if not created:
            return _duplicate_outcome(s, src)
        s.flush()
    with services.session() as s:
        src = s.get(Source, src.id)
        eid = _queue_ingestion(services, ws_id, src, {"input_path": rel, "input_sha256": sha, "filename": filename},
                               f"ingest:{src.id}:{sha}", {"type": "upload", "filename": filename, "bytes": len(data)})
    return SubmitResult(src.id, eid, False, "queued", "Queued for processing.")


def submit_url(services: Services, ws_id: str, url: str, refresh: bool = False) -> SubmitResult:
    normalized, _, _, _ = validate_url(url, services.fetch_resolver or default_resolver)
    ident = canonical_url(normalized)
    with services.session() as s:
        _workspace(s, ws_id)
        src, created = _get_or_create_source(s, ws_id, "url", f"url:{ident}", ident, ident)
        if not created and not (refresh and src.status in ("completed", "partial", "failed")):
            return _duplicate_outcome(s, src)
        nonce = "initial" if created else uuid.uuid4().hex[:8]
        if not created:
            src.status = "queued"
        sid = src.id
    with services.session() as s:
        src = s.get(Source, sid)
        eid = _queue_ingestion(services, ws_id, src, {"url": ident}, f"ingest:{sid}:{nonce}", {"type": "url", "url": ident, "refresh": not created})
    return SubmitResult(sid, eid, False, "queued", "Queued for processing." if created else "Re-fetch queued.")


def submit_note(services: Services, ws_id: str, text: str, title: str | None, source_id: str | None = None) -> SubmitResult:
    extract_note(text, title, services.settings.max_note_chars)  # validates emptiness / size; raises InvalidInput
    raw = text.replace("\r\n", "\n")
    rel, sha = store_original(services, raw.encode("utf-8"), ".md")
    with services.session() as s:
        _workspace(s, ws_id)
        if source_id is None:
            src, created = _get_or_create_source(s, ws_id, "note", f"note:{uuid.uuid4().hex}", title or "Untitled note", None)
        else:
            src = s.query(Source).filter_by(id=source_id, workspace_id=ws_id, kind="note").first()
            if src is None:
                raise NotFound("note not found in this workspace")
            created = False
            ver = s.query(SourceVersion).filter_by(source_id=src.id, version=src.current_version).first()
            if ver is not None and ver.meta.get("raw_sha256") == sha and (title is None or title == src.title) and src.status == "completed":
                return SubmitResult(src.id, None, True, "unchanged", "The note content is unchanged; no new version created.")
            if title:
                src.title = title[:500]
            src.status = "queued"
        sid = src.id
    with services.session() as s:
        src = s.get(Source, sid)
        eid = _queue_ingestion(services, ws_id, src, {"input_path": rel, "input_sha256": sha, "note_title": title},
                               f"ingest:{sid}:{sha}", {"type": "note", "edit": not created, "chars": len(raw)})
    return SubmitResult(sid, eid, False, "queued", "Queued for processing.")


def retry_source(services: Services, source_id: str) -> str:
    """Resume the latest failed/interrupted ingestion of a source; never creates duplicate side effects."""
    with services.session() as s:
        last = _latest_execution(s, source_id)
        if last is None:
            raise NotFound("no ingestion execution recorded for this source")
        eid, st = last.id, last.status
    if st in ("failed", "interrupted"):
        services.runner.resume(eid)
        return eid
    raise InvalidInput(f"latest execution is '{st}', nothing to retry", code="not_retryable")
