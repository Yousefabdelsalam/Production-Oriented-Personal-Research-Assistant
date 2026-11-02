"""DB-backed job queue + in-process worker thread.

Why not Redis/Celery: ingestion is low-volume but slow (Hindsight retain ~10-15 s); the persisted execution row
already *is* the job record, and an atomic UPDATE claims it. Crash recovery = rows left 'running' become
'interrupted' at startup and are resumed (bounded by WORKER_AUTO_RESUME_MAX) from their last checkpoint.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import select, update

from app.container import Services
from app.models import WorkflowExecution

log = logging.getLogger("worker")


class JobWorker:
    def __init__(self, services: Services, poll_s: float = 1.0):
        self.services = services
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        n = self.services.runner.recover_interrupted()
        if n:
            log.warning("recovered interrupted executions", extra={"count": n})
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def run_pending_once(self) -> int:
        """Process all currently runnable executions synchronously (used by tests and the loop)."""
        done = 0
        while (eid := self._next_runnable()) is not None:
            try:
                self.services.runner.run(eid)
            except Exception:  # noqa: BLE001 - runner records failures itself; this is a last resort
                log.exception("worker crashed running execution", extra={"execution_id": eid})
            done += 1
        return done

    def _next_runnable(self) -> str | None:
        r = self.services.runner
        max_auto = self.services.settings.worker_auto_resume_max
        with self.services.session() as s:
            row = s.execute(
                select(WorkflowExecution.id).where(WorkflowExecution.status == "queued").order_by(WorkflowExecution.created_at).limit(1)
            ).first()
            if row is None:
                row = s.execute(
                    select(WorkflowExecution.id)
                    .where(WorkflowExecution.status == "interrupted", WorkflowExecution.auto_resumes < max_auto)
                    .order_by(WorkflowExecution.created_at).limit(1)
                ).first()
                if row is not None:
                    s.execute(update(WorkflowExecution).where(WorkflowExecution.id == row[0]).values(auto_resumes=WorkflowExecution.auto_resumes + 1))
        if row is None:
            return None
        eid = row[0]
        if r.claim(eid, ("queued", "interrupted")):
            return eid
        return self._next_runnable() if not self._stop.is_set() else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_pending_once() == 0:
                    self._stop.wait(self.poll_s)
            except Exception:  # noqa: BLE001
                log.exception("worker loop error")
                self._stop.wait(self.poll_s)
