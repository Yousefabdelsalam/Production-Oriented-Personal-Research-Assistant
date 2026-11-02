"""Dependency container passed to workflows. Keeps the harness free of globals so tests can swap parts."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.harness.logging import sanitize_detail
from app.models import MemoryOperation
from app.services.hindsight_gateway import HindsightGateway
from app.services.llm import ModelRuntime


@dataclass
class Services:
    settings: Settings
    session_factory: sessionmaker[Session]
    gateway: HindsightGateway
    models: ModelRuntime
    sleep: Callable[[float], None] = time.sleep
    # fetch hooks are injectable so tests can exercise the real SSRF logic with a fake network
    fetch_transport: Any = None
    fetch_resolver: Any = None
    runner: Any = None  # WorkflowRunner, set by app wiring (circular import avoidance)
    extras: dict[str, Any] = field(default_factory=dict)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---- Hindsight side-effect ledger (own transaction so it survives stage rollbacks) -------------------
    def ledger_find(self, key: str) -> MemoryOperation | None:
        with self.session() as s:
            row = s.query(MemoryOperation).filter_by(idempotency_key=key).first()
            if row is not None:
                s.expunge(row)
            return row

    def ledger_record(self, *, key: str | None, **fields: Any) -> MemoryOperation:
        for k in ("request", "response"):
            if k in fields:
                fields[k] = sanitize_detail(fields[k])
        try:
            with self.session() as s:
                row = MemoryOperation(idempotency_key=key, **fields)
                s.add(row)
                s.flush()
                s.expunge(row)
                return row
        except IntegrityError:
            existing = self.ledger_find(key) if key else None
            if existing is None:
                raise
            return existing
