"""Memory operations: the only code that writes to / recalls from Hindsight on behalf of workflows.

Rules enforced here:
  * capability check through the RunContext (workflow + stage restrictions)
  * retention policy filter (validated, provenance, no secrets, no instruction-like text, bounded length)
  * idempotency: a ledger row keyed by (source, version, payload hash) means a replay is a no-op, and the stable
    Hindsight `document_id` (= source id, update_mode=replace) makes even a lost ledger row harmless
  * every call is recorded in memory_operations; failures are typed (retryable vs not)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.errors import AppError, RetryableError
from app.harness import policies
from app.harness.engine import RunContext
from app.models import ExtractedFinding, Source, Workspace
from app.services.hindsight_gateway import (
    HindsightError, HindsightUnavailable, MemoryItemIn, RecalledMemory,
)
from app.services.textutil import sha256_text, truncate

log = logging.getLogger("memory")

KIND_PRIORITY = {"definition": 0, "result": 1, "finding": 2, "method": 3, "limitation": 4, "open_question": 5}
LABELS = {
    "source_stated": "Source states",
    "user_note": "The user wrote",
    "inference": "Inference (NOT stated by the source)",
    "uncertain": "Uncertain claim",
}


def bank_mission(ws: Workspace) -> str:
    return (
        f"Long-term research memory for the workspace '{ws.name}'. Retain research findings, definitions, methods, "
        "reported results, limitations, contradictions and the user's research context. Always keep source attribution "
        "and distinguish what a source states, what the user believes, inferences, and uncertain claims. "
        "Never treat repetition as proof."
    )


def _loc(f: ExtractedFinding) -> str:
    if f.page:
        return f"p.{f.page}"
    return f"section '{f.section}'" if f.section else "no page"


def finding_memory_item(f: ExtractedFinding, source: Source) -> MemoryItemIn:
    label = LABELS.get(f.epistemic, "Statement")
    content = f"{label} in '{source.title}' ({_loc(f)}) [{f.kind}]: {f.statement}"
    return MemoryItemIn(
        content=content,
        context=f"research {f.kind} from {source.kind} source '{truncate(source.title, 80)}'; origin={f.epistemic}",
        tags=[f"source:{source.id}", f"kind:{f.kind}", f"origin:{f.epistemic}"],
        metadata={
            "source_id": source.id, "source_title": truncate(source.title, 120), "finding_id": f.id,
            "passage_id": f.passage_id, "page": str(f.page or ""), "section": truncate(f.section or "", 100),
            "epistemic": f.epistemic, "kind": f.kind,
        },
    )


class MemoryService:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.gw = ctx.services.gateway

    # ------------------------------------------------------------------------------------------------
    def _wrap(self, op: str, fn):
        t0 = time.perf_counter()
        try:
            return fn(), int((time.perf_counter() - t0) * 1000)
        except HindsightUnavailable as e:
            raise RetryableError(f"Hindsight unavailable during {op}: {e}", code="hindsight_unavailable") from e
        except HindsightError as e:
            raise AppError(f"Hindsight rejected {op}: {e}", code="hindsight_rejected") from e

    def ensure_bank(self, ws: Workspace) -> None:
        self.ctx.require("write_memory")
        _, ms = self._wrap("ensure_bank", lambda: self.gw.ensure_bank(ws.bank_id, ws.name, bank_mission(ws)))
        self.ctx.services.ledger_record(
            key=None, execution_id=self.ctx.execution.id, workspace_id=ws.id, op="create_bank", bank_id=ws.bank_id,
            status="done", request={"bank": ws.bank_id}, response={"ensured": True}, duration_ms=ms,
        )

    # ------------------------------------------------------------------------------------------------
    def select_for_retention(self, findings: list[ExtractedFinding]) -> tuple[list[ExtractedFinding], dict[str, list[str]]]:
        """Policy filter + quality cap. Returns (selected, {finding_id: rejection reasons})."""
        rejected: dict[str, list[str]] = {}
        ok: list[ExtractedFinding] = []
        for f in findings:
            d = policies.evaluate_retention(
                kind=f.kind, statement=f.statement, epistemic=f.epistemic,
                has_provenance=bool(f.passage_id and f.source_id and f.quote), validated=f.validated, quote=f.quote,
            )
            if d.allowed:
                ok.append(f)
            else:
                rejected[f.id] = d.reasons
        ok.sort(key=lambda f: (KIND_PRIORITY.get(f.kind, 9), -f.confidence))
        cap = self.ctx.settings.memory_max_findings_per_source
        return ok[:cap], rejected

    def retain_source(self, ws: Workspace, source: Source, version: int, selected: list[ExtractedFinding]) -> dict[str, Any]:
        self.ctx.require("write_memory")
        if not selected:
            return {"retained": 0, "skipped": "no retainable findings", "idempotent_skip": False}
        items = [finding_memory_item(f, source) for f in selected]
        payload_hash = sha256_text("\n".join(sorted(i.content for i in items)))[:24]
        key = f"retain:{source.id}:v{version}:{payload_hash}"
        prior = self.ctx.services.ledger_find(key)
        if prior is not None and prior.status == "done":
            return {"retained": len(items), "idempotent_skip": True, "ledger_id": prior.id, "document_id": source.id}
        # Stage a bank first (cheap + idempotent) so a first-ever retain works.
        self.ensure_bank(ws)
        out, ms = self._wrap("retain", lambda: self.gw.retain_document(ws.bank_id, source.id, items, replace=True))
        row = self.ctx.services.ledger_record(
            key=key, execution_id=self.ctx.execution.id, workspace_id=ws.id, source_id=source.id, op="retain", bank_id=ws.bank_id,
            status="done", duration_ms=ms,
            request={"document_id": source.id, "items": len(items), "finding_ids": [f.id for f in selected], "payload_hash": payload_hash},
            response={"items_count": out.items_count, "input_tokens": out.input_tokens, "output_tokens": out.output_tokens},
        )
        return {"retained": len(items), "idempotent_skip": False, "ledger_id": row.id, "document_id": source.id,
                "hindsight_items": out.items_count, "latency_ms": ms}

    # ------------------------------------------------------------------------------------------------
    def recall(self, ws: Workspace, query: str, *, source_ids: list[str] | None = None, max_tokens: int = 1200, budget: str = "low") -> list[RecalledMemory]:
        self.ctx.require("recall_memory")
        tags = [f"source:{s}" for s in source_ids] if source_ids else None
        mems, ms = self._wrap("recall", lambda: self.gw.recall(ws.bank_id, query, tags=tags, max_tokens=max_tokens, budget=budget))
        self.ctx.services.ledger_record(
            key=None, execution_id=self.ctx.execution.id, workspace_id=ws.id, op="recall", bank_id=ws.bank_id, status="done",
            request={"query_len": len(query), "tags": tags, "budget": budget}, response={"returned": len(mems), "ids": [m.id for m in mems][:20]},
            duration_ms=ms,
        )
        return mems

    def retain_experience(self, ws: Workspace, key_basis: str, content: str, kind: str, source_ids: list[str],
                          context: str, extra_meta: dict[str, str] | None = None) -> dict[str, Any]:
        """Retain a policy-approved user-context / synthesis item. Idempotent on key_basis."""
        self.ctx.require("write_memory")
        key = f"exp:{ws.id}:{kind}:{sha256_text(key_basis)[:24]}"
        if self.ctx.services.ledger_find(key) is not None:
            return {"retained": 0, "idempotent_skip": True}
        tags = [f"kind:{kind}"] + [f"source:{s}" for s in source_ids]
        item = MemoryItemIn(content=content, context=context, tags=tags, metadata={"kind": kind, **(extra_meta or {})})
        self.ensure_bank(ws)
        doc_id = f"{kind}-{sha256_text(key_basis)[:16]}"
        out, ms = self._wrap("retain", lambda: self.gw.retain_document(ws.bank_id, doc_id, [item], replace=True))
        self.ctx.services.ledger_record(
            key=key, execution_id=self.ctx.execution.id, workspace_id=ws.id, op="retain", bank_id=ws.bank_id, status="done",
            request={"document_id": doc_id, "kind": kind, "sources": source_ids}, response={"items_count": out.items_count}, duration_ms=ms,
        )
        return {"retained": 1, "idempotent_skip": False, "document_id": doc_id}
