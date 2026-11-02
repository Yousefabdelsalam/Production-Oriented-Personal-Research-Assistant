"""Persisted workflow runner.

A workflow is a small explicit state machine: named stages, each with a handler, a set of allowed successor
stages and a retry budget. The runner:
  * checkpoints typed state after every successful stage, in the SAME transaction as the stage's DB writes
  * records one WorkflowStep per attempt (status, duration, trace detail, error)
  * enforces the declared transitions (an undeclared `goto` is an error, not a jump)
  * retries only `RetryableError`s, with bounded exponential backoff
  * on failure stops at the failed stage; `resume()` restarts from it using the persisted state, so completed
    stages (and their side effects / model calls) are never repeated
Handlers propose; this module decides what runs next.

SQLite rule for handlers: do not hold pending writes in `ctx.session` while calling `services.ledger_record`
(a separate connection). Gather -> call external system -> record ledger -> THEN write via ctx.session.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.container import Services
from app.errors import AppError, IllegalTransition, PolicyViolation, RetryableError
from app.harness import policies
from app.harness.logging import correlation_id, execution_id as execution_id_var, redact, sanitize_detail, stage_name
from app.models import WorkflowExecution, WorkflowStep, utcnow
from app.services.llm import Usage

log = logging.getLogger("harness")

Handler = Callable[["RunContext"], "StageResult | None"]


@dataclass
class StageResult:
    goto: str | None = None


@dataclass
class Stage:
    name: str
    handler: Handler
    next: tuple[str, ...] = ()
    retries: int = 0


@dataclass
class WorkflowDef:
    name: str
    state_model: type[BaseModel]
    stages: dict[str, Stage]
    start: str
    on_failure: Callable[["RunContext", Exception], None] | None = None
    on_success: Callable[["RunContext"], None] | None = None

    def __post_init__(self) -> None:
        if self.start not in self.stages:
            raise ValueError(f"{self.name}: start stage '{self.start}' not defined")
        for st in self.stages.values():
            for n in st.next:
                if n not in self.stages:
                    raise ValueError(f"{self.name}: stage '{st.name}' declares unknown successor '{n}'")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "start": self.start,
                "stages": [{"name": s.name, "next": list(s.next), "retries": s.retries} for s in self.stages.values()]}


class RunContext:
    """What a stage handler can see and do. Handlers write trace data via `trace()`/`detail`, never to logs directly."""

    def __init__(self, services: Services, session: Session, execution: WorkflowExecution, state: BaseModel, stage: str, attempt: int):
        self.services = services
        self.session = session
        self.execution = execution
        self.state = state
        self.stage = stage
        self.attempt = attempt
        self.detail: dict[str, Any] = {}
        self.model_usage: list[Usage] = []
        self.result: dict[str, Any] | None = None

    @property
    def settings(self):
        return self.services.settings

    @property
    def workflow(self) -> str:
        return self.execution.workflow

    @property
    def correlation_id(self) -> str:
        return self.execution.correlation_id

    def require(self, capability: str) -> None:
        policies.require(self.workflow, self.stage, capability)

    def trace(self, key: str, value: Any) -> None:
        self.detail[key] = value

    def add_to_trace(self, key: str, item: Any) -> None:
        self.detail.setdefault(key, []).append(item)

    def record_usage(self, usage: Usage) -> None:
        self.model_usage.append(usage)

    def validation(self, name: str, passed: bool, info: Any = None) -> None:
        self.add_to_trace("validations", {"name": name, "passed": passed, "info": info})


def _utc(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class WorkflowRunner:
    def __init__(self, services: Services, definitions: dict[str, WorkflowDef]):
        self.services = services
        self.defs = definitions
        services.runner = self

    # ---- creation ---------------------------------------------------------------------------------
    def create(self, workflow: str, *, state: BaseModel, workspace_id: str | None = None, source_id: str | None = None,
               trigger: dict[str, Any] | None = None, idempotency_key: str | None = None,
               correlation: str | None = None, status: str = "queued") -> str:
        d = self.defs[workflow]
        with self.services.session() as s:
            if idempotency_key:
                existing = s.query(WorkflowExecution).filter_by(idempotency_key=idempotency_key).first()
                if existing is not None:
                    return existing.id
            ex = WorkflowExecution(
                workflow=workflow, workspace_id=workspace_id, source_id=source_id,
                correlation_id=correlation or f"cor_{uuid.uuid4().hex[:12]}",
                status=status, current_stage=d.start, trigger=sanitize_detail(trigger or {}),
                idempotency_key=idempotency_key, state=state.model_dump(mode="json"),
            )
            s.add(ex)
            s.flush()
            return ex.id

    # ---- claim / resume ------------------------------------------------------------------------------
    def claim(self, execution_id: str, from_status: tuple[str, ...] = ("queued",)) -> bool:
        with self.services.session() as s:
            res = s.execute(
                update(WorkflowExecution)
                .where(WorkflowExecution.id == execution_id, WorkflowExecution.status.in_(from_status))
                .values(status="running", started_at=utcnow(), error=None)
            )
            return res.rowcount == 1

    def resume(self, execution_id: str) -> bool:
        """Re-queue a failed/interrupted execution; it restarts at the stage that failed with its saved state."""
        with self.services.session() as s:
            res = s.execute(
                update(WorkflowExecution)
                .where(WorkflowExecution.id == execution_id, WorkflowExecution.status.in_(("failed", "interrupted")))
                .values(status="queued", resume_count=WorkflowExecution.resume_count + 1, failed_stage=None, error=None, finished_at=None)
            )
            return res.rowcount == 1

    def recover_interrupted(self) -> int:
        """Called at startup: anything left 'running' belonged to a dead process."""
        with self.services.session() as s:
            ids = [r[0] for r in s.execute(select(WorkflowExecution.id).where(WorkflowExecution.status == "running"))]
            if not ids:
                return 0
            # the attempt that was in flight when the process died must not stay 'running' in the trace forever
            s.execute(
                update(WorkflowStep).where(WorkflowStep.execution_id.in_(ids), WorkflowStep.status == "running")
                .values(status="failed", finished_at=utcnow(),
                        error="Interrupted: the process stopped while this stage was running; it is re-run from the last checkpoint.")
            )
            s.execute(update(WorkflowExecution).where(WorkflowExecution.id.in_(ids)).values(status="interrupted"))
            return len(ids)

    # ---- execution ---------------------------------------------------------------------------------
    def run(self, execution_id: str) -> WorkflowExecution:
        """Run (or continue) an execution already claimed as 'running'. Returns the final row."""
        if not self._is_running(execution_id):
            if not self.claim(execution_id):
                raise AppError(f"execution {execution_id} is not runnable", code="not_runnable")
        tok_c = tok_e = None
        with self.services.session() as s:
            ex = s.get(WorkflowExecution, execution_id)
            d = self.defs[ex.workflow]
            stage_cursor = ex.current_stage or d.start
            began = _utc(ex.started_at) or utcnow()
            corr = ex.correlation_id
        tok_c = correlation_id.set(corr)
        tok_e = execution_id_var.set(execution_id)
        log.info("workflow.start", extra={"workflow": d.name, "resume_stage": stage_cursor})
        t_all = time.perf_counter()
        try:
            while True:
                nxt = self._run_stage(d, execution_id, stage_cursor)
                if nxt is None:
                    break
                stage_cursor = nxt
        finally:
            correlation_id.reset(tok_c)
            execution_id_var.reset(tok_e)
            stage_name.set("-")
        with self.services.session() as s:
            ex = s.get(WorkflowExecution, execution_id)
            ex.duration_ms = (ex.duration_ms or 0) + int((time.perf_counter() - t_all) * 1000)
            s.flush()
            s.expunge(ex)
        return ex

    def _is_running(self, execution_id: str) -> bool:
        with self.services.session() as s:
            ex = s.get(WorkflowExecution, execution_id)
            return ex is not None and ex.status == "running"

    def _run_stage(self, d: WorkflowDef, execution_id: str, stage: str) -> str | None:
        """Runs one stage with retries. Returns next stage name, or None when the execution ended (success or failure)."""
        st = d.stages[stage]
        attempt = 0
        while True:
            attempt += 1
            stage_name.set(stage)
            # 1) visible 'running' step row (own transaction so the inspector shows in-flight work)
            with self.services.session() as s:
                step = WorkflowStep(execution_id=execution_id, stage=stage, attempt=attempt, status="running")
                s.add(step)
                s.flush()
                step_id = step.id
                ex = s.get(WorkflowExecution, execution_id)
                ex.current_stage = stage
                state = d.state_model.model_validate(ex.state)  # fresh from last checkpoint on every attempt
            t0 = time.perf_counter()
            s = self.services.session_factory()
            ctx: RunContext | None = None
            try:
                ex = s.get(WorkflowExecution, execution_id)
                ctx = RunContext(self.services, s, ex, state, stage, attempt)
                result = st.handler(ctx)
                nxt = (result.goto if result and result.goto else (st.next[0] if st.next else None))
                if nxt is not None and nxt not in st.next:
                    raise IllegalTransition(f"{d.name}: {stage} -> {nxt} is not a declared transition {list(st.next)}")
                if nxt is None and st.next:
                    raise IllegalTransition(f"{d.name}: stage {stage} must transition to one of {list(st.next)}")
                # 2) atomic checkpoint: handler writes + step + state + pointer commit together
                dur = int((time.perf_counter() - t0) * 1000)
                stp = s.get(WorkflowStep, step_id)
                stp.status, stp.finished_at, stp.duration_ms = "succeeded", utcnow(), dur
                stp.detail = sanitize_detail({**ctx.detail, "model_calls": [u.as_dict() for u in ctx.model_usage], "next": nxt})
                ex.state = ctx.state.model_dump(mode="json")
                ex.current_stage = nxt or stage
                ex.tokens_in += sum(u.input_tokens for u in ctx.model_usage)
                ex.tokens_out += sum(u.output_tokens for u in ctx.model_usage)
                ex.model_calls += len(ctx.model_usage)
                if nxt is None:
                    ex.status, ex.finished_at = "succeeded", utcnow()
                    # results are user-facing payloads (e.g. the full answer): redact secrets but do not truncate like trace detail
                    ex.result = sanitize_detail(ctx.result if ctx.result is not None else {}, max_str=100_000)
                    if d.on_success:
                        d.on_success(ctx)
                s.commit()
                log.info("stage.ok", extra={"stage": stage, "attempt": attempt, "duration_ms": dur})
                return nxt
            except Exception as e:  # noqa: BLE001 - classified below, never swallowed
                s.rollback()
                dur = int((time.perf_counter() - t0) * 1000)
                retryable = isinstance(e, (RetryableError,)) or (isinstance(e, AppError) and e.retryable)
                will_retry = retryable and attempt <= st.retries
                self._record_failure(execution_id, step_id, e, dur, will_retry, ctx.detail if ctx else None)
                log.warning("stage.failed", extra={"stage": stage, "attempt": attempt, "duration_ms": dur,
                                                     "error_type": type(e).__name__, "error": redact(str(e))[:300], "will_retry": will_retry})
                if will_retry:
                    self.services.sleep(self.services.settings.retry_backoff_base_s * (2 ** (attempt - 1)))
                    s.close()
                    continue
                self._fail_execution(d, execution_id, stage, e)
                return None
            finally:
                s.close()

    def _record_failure(self, execution_id: str, step_id: int, e: Exception, dur: int, will_retry: bool, partial: dict[str, Any] | None = None) -> None:
        with self.services.session() as s:
            stp = s.get(WorkflowStep, step_id)
            stp.status, stp.finished_at, stp.duration_ms = "failed", utcnow(), dur
            stp.error = f"{type(e).__name__}: {redact(str(e))[:800]}"
            # keep whatever the handler traced before failing (e.g. the validation results that caused the failure)
            stp.detail = sanitize_detail({**(partial or {}), "will_retry": will_retry, "error_code": getattr(e, "code", "unexpected")})

    def _fail_execution(self, d: WorkflowDef, execution_id: str, stage: str, e: Exception) -> None:
        with self.services.session() as s:
            ex = s.get(WorkflowExecution, execution_id)
            ex.status, ex.failed_stage, ex.finished_at = "failed", stage, utcnow()
            ex.error = f"{type(e).__name__}: {redact(str(e))[:1000]}"
            ex.current_stage = stage  # resume restarts here
        if d.on_failure:
            # failure hook runs in its own transaction so a hook bug cannot hide the original error
            try:
                with self.services.session() as s:
                    ex = s.get(WorkflowExecution, execution_id)
                    state = d.state_model.model_validate(ex.state)
                    d.on_failure(RunContext(self.services, s, ex, state, stage, 0), e)
            except Exception:  # noqa: BLE001
                log.exception("workflow.on_failure hook failed")
        log.error("workflow.failed", extra={"workflow": d.name, "failed_stage": stage, "error_type": type(e).__name__})

    # ---- convenience ----------------------------------------------------------------------------------
    def start_and_run(self, workflow: str, **kwargs: Any) -> WorkflowExecution:
        """Create + run synchronously (used by request/response workflows like QA)."""
        exec_id = self.create(workflow, status="running", **kwargs)
        if not self._is_running(exec_id):  # idempotency key matched an existing execution: return it untouched
            with self.services.session() as s:
                ex = s.get(WorkflowExecution, exec_id)
                s.expunge(ex)
                return ex
        return self.run(exec_id)
