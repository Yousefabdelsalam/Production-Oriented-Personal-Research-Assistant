"""Shared test helpers: PDF generation, ingestion driving."""
from __future__ import annotations

from app.container import Services
from app.harness.worker import JobWorker
from app.models import WorkflowExecution
from app.services import sources as src_svc
from evaldata.pdfgen import make_pdf  # noqa: F401  (re-exported for tests)


def drain(services: Services) -> int:
    """Run all queued executions (what the background worker thread does)."""
    return JobWorker(services).run_pending_once()


def ingest_pdf(services: Services, ws_id: str, name: str, pages: list[str], title: str | None = None):
    res = src_svc.submit_pdf(services, ws_id, name, make_pdf(pages, title))
    drain(services)
    return res


def ingest_note(services: Services, ws_id: str, text: str, title: str | None = None, source_id: str | None = None):
    res = src_svc.submit_note(services, ws_id, text, title, source_id)
    drain(services)
    return res


def get_execution(services: Services, exec_id: str) -> WorkflowExecution:
    with services.session() as s:
        ex = s.get(WorkflowExecution, exec_id)
        _ = ex.steps
        s.expunge_all()
        return ex
