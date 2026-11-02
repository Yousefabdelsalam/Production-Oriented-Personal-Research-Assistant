"""TEST-ONLY stand-ins. Nothing here is evidence that the real Hindsight/LLM integration works —
that is what tests/test_hindsight_live.py and the `hindsight`-marked e2e tests are for.

InMemoryHindsightDouble mimics the *behaviour we verified live*: bank creation is idempotent, retain with a
document_id and replace semantics replaces that document's memories, recall filters by tags.
"""
from __future__ import annotations

import itertools
import re
from typing import Any

from app.services.hindsight_gateway import (
    HindsightError, HindsightHealth, HindsightUnavailable, MemoryItemIn, RecalledMemory, RetainOutcome, validate_bank_id,
)
from app.services.llm import LLMError, Usage

_ids = itertools.count(1)


class InMemoryHindsightDouble:
    is_test_double = True  # the evaluation suite refuses to report this as a live Hindsight

    def __init__(self):
        self.banks: dict[str, dict[str, list[RecalledMemory]]] = {}
        self.retain_calls: list[tuple[str, str, int]] = []
        self.recall_calls: list[tuple[str, str, list[str] | None]] = []
        self.consolidations: list[str] = []
        self.down = False

    def _check(self):
        if self.down:
            raise HindsightUnavailable("double: simulated outage")

    def health(self) -> HindsightHealth:
        return HindsightHealth(ok=not self.down, version="double", detail=None if not self.down else "outage")

    def ensure_bank(self, bank_id, name, mission):
        self._check()
        validate_bank_id(bank_id)
        self.banks.setdefault(bank_id, {})
        return True

    def retain_document(self, bank_id, document_id, items: list[MemoryItemIn], replace=True):
        self._check()
        validate_bank_id(bank_id)
        if bank_id not in self.banks:
            raise HindsightError("bank does not exist")
        self.retain_calls.append((bank_id, document_id, len(items)))
        mems = [
            RecalledMemory(id=f"mem_{next(_ids)}", text=i.content, type="world", tags=list(i.tags), metadata=dict(i.metadata),
                           document_id=document_id, context=i.context)
            for i in items
        ]
        if replace:
            self.banks[bank_id][document_id] = mems
        else:
            self.banks[bank_id].setdefault(document_id, []).extend(mems)
        return RetainOutcome(items_count=len(mems), document_id=document_id)

    def recall(self, bank_id, query, tags=None, max_tokens=1500, budget="low"):
        self._check()
        validate_bank_id(bank_id)
        self.recall_calls.append((bank_id, query, tags))
        qt = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored = []
        for mems in self.banks.get(bank_id, {}).values():
            for m in mems:
                if tags and not (set(tags) & set(m.tags)):
                    continue
                overlap = len(qt & set(re.findall(r"[a-z0-9]+", m.text.lower())))
                if overlap:
                    scored.append((overlap, m))
        scored.sort(key=lambda t: -t[0])
        return [m for _, m in scored[:10]]

    def list_document_memories(self, bank_id, search_query=None, limit=50):
        self._check()
        return [m for mems in self.banks.get(bank_id, {}).values() for m in mems][:limit]

    def consolidate(self, bank_id, model_id, name, source_query):
        self._check()
        self.consolidations.append(bank_id)
        return {"mental_model_id": model_id, "created": True, "refreshed": False}

    def get_consolidated(self, bank_id, model_id):
        self._check()
        if bank_id not in self.consolidations:
            return None
        return {"id": model_id, "name": "double", "content": "(test double: no consolidation performed)", "is_stale": False,
                "last_refreshed_at": None, "last_refresh_failed_at": None, "source_query": ""}

    def delete_bank(self, bank_id):
        self.banks.pop(bank_id, None)

    def close(self):
        pass

    # helpers for assertions
    def all_texts(self, bank_id: str) -> list[str]:
        return [m.text for mems in self.banks.get(bank_id, {}).values() for m in mems]


class FlakyGateway:
    """Wraps a gateway; raises HindsightUnavailable for the first `fail_first` retain calls (a temporary outage)."""

    def __init__(self, inner, fail_first: int = 1, fail_recall: bool = False):
        self.inner, self.remaining, self.fail_recall = inner, fail_first, fail_recall
        self.retain_attempts = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def retain_document(self, *a, **k):
        self.retain_attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise HindsightUnavailable("flaky: temporary outage")
        return self.inner.retain_document(*a, **k)

    def recall(self, *a, **k):
        if self.fail_recall:
            raise HindsightUnavailable("flaky: recall outage")
        return self.inner.recall(*a, **k)


class ScriptedLLM:
    """TEST-ONLY LLM: returns queued JSON-able objects or calls a function. Records every call (purpose, user prompt)."""
    provider = "scripted-test-double"
    model = "scripted"

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, purpose, system, user, schema, max_tokens=2000):
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        out = self.responder(purpose, user)
        if out is None:
            raise LLMError("scripted: no response")
        return schema.model_validate(out), Usage(self.provider, self.model, purpose, 10, 5, 1)
