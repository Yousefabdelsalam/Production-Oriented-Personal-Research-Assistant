"""Workspace management + dashboard statistics."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import InvalidInput, NotFound
from app.models import EvidencePassage, ExtractedFinding, ResearchRelationship, Source, WorkflowExecution, Workspace
from app.services.hindsight_gateway import validate_bank_id
from app.services.textutil import slugify


def create_workspace(s: Session, settings: Settings, name: str, description: str = "", slug: str | None = None) -> Workspace:
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise InvalidInput("Workspace name is required (max 200 characters).", code="bad_name")
    base = slugify(slug or name, 40)
    for i in range(0, 50):
        cand = base if i == 0 else f"{base}-{i + 1}"
        bank = settings.hindsight_bank_prefix + cand
        try:
            validate_bank_id(bank)
        except Exception as e:  # noqa: BLE001
            raise InvalidInput(f"Cannot derive a valid Hindsight bank id: {e}", code="bad_bank_id") from e
        if s.query(Workspace).filter((Workspace.slug == cand) | (Workspace.bank_id == bank)).first() is None:
            ws = Workspace(slug=cand, name=name, description=description or "", bank_id=bank)
            try:
                with s.begin_nested():
                    s.add(ws)
                    s.flush()
                return ws
            except IntegrityError:
                continue
    raise InvalidInput("Could not allocate a unique workspace slug.", code="slug_exhausted")


def get_workspace(s: Session, ws_id: str) -> Workspace:
    ws = s.get(Workspace, ws_id)
    if ws is None:
        raise NotFound(f"workspace {ws_id} not found")
    return ws


def workspace_stats(s: Session, ws: Workspace) -> dict:
    by_status = dict(s.query(Source.status, func.count()).filter(Source.workspace_id == ws.id).group_by(Source.status).all())
    active = (s.query(WorkflowExecution).filter(WorkflowExecution.workspace_id == ws.id, WorkflowExecution.status.in_(("queued", "running"))).count())
    failed = (s.query(WorkflowExecution).filter(WorkflowExecution.workspace_id == ws.id, WorkflowExecution.status.in_(("failed", "interrupted"))).count())
    return {
        "id": ws.id, "slug": ws.slug, "name": ws.name, "description": ws.description, "bank_id": ws.bank_id,
        "created_at": ws.created_at, "updated_at": ws.updated_at, "summary_version": ws.current_summary_version,
        "sources": sum(by_status.values()), "sources_by_status": by_status,
        "passages": s.query(EvidencePassage).filter_by(workspace_id=ws.id, is_current=True).count(),
        "findings": s.query(ExtractedFinding).filter_by(workspace_id=ws.id, validated=True, superseded=False).count(),
        "relationships": s.query(ResearchRelationship).filter_by(workspace_id=ws.id, active=True).count(),
        "jobs_active": active, "jobs_failed": failed,
    }
