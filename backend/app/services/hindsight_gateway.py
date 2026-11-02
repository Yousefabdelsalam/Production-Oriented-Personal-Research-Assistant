"""Integration boundary to Hindsight (hindsight-client 0.10.2).

Only methods verified against the installed client and a live server are used:
create_bank / get_bank_config, retain_batch(document_id, items[update_mode]), recall(tags, tags_match, types),
list_memories, create_mental_model / refresh_mental_model / get_mental_model, get_version, delete_bank.

Errors are mapped to two classes so the harness can decide whether to retry:
  HindsightUnavailable  -> connection refused / timeout / 5xx   (retryable)
  HindsightError        -> 4xx / unexpected response            (not retryable)

No in-memory substitute lives here. Tests that need a stand-in use tests/doubles.py and never claim
that it exercises the real integration.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("hindsight")

BANK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class HindsightError(Exception):
    """Non-retryable Hindsight failure (bad request, unexpected payload)."""


class HindsightUnavailable(HindsightError):
    """Hindsight cannot be reached or is failing server-side (retryable)."""


class HindsightBankNotFound(HindsightError):
    """The bank does not exist on this Hindsight server (yet). For reads this means 'no memories', not an outage."""


@dataclass
class MemoryItemIn:
    content: str
    context: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class RecalledMemory:
    id: str
    text: str
    type: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    document_id: str | None = None
    context: str | None = None

    def source_ids(self) -> list[str]:
        out = [t.split(":", 1)[1] for t in self.tags if t.startswith("source:")]
        sid = self.metadata.get("source_id")
        if sid and sid not in out:
            out.append(sid)
        return out


@dataclass
class RetainOutcome:
    items_count: int
    document_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@dataclass
class HindsightHealth:
    ok: bool
    version: str | None = None
    detail: str | None = None


def validate_bank_id(bank_id: str) -> str:
    if not BANK_ID_RE.match(bank_id):
        raise HindsightError(f"invalid bank id {bank_id!r}: must match {BANK_ID_RE.pattern}")
    return bank_id


class HindsightGateway(Protocol):
    def health(self) -> HindsightHealth: ...
    def ensure_bank(self, bank_id: str, name: str, mission: str) -> bool: ...
    def retain_document(
        self, bank_id: str, document_id: str, items: list[MemoryItemIn], replace: bool = True
    ) -> RetainOutcome: ...
    def recall(
        self, bank_id: str, query: str, tags: list[str] | None = None, max_tokens: int = 1500, budget: str = "low"
    ) -> list[RecalledMemory]: ...
    def list_document_memories(self, bank_id: str, search_query: str | None = None, limit: int = 50) -> list[RecalledMemory]: ...
    def consolidate(self, bank_id: str, model_id: str, name: str, source_query: str) -> dict[str, Any]: ...
    def get_consolidated(self, bank_id: str, model_id: str) -> dict[str, Any] | None: ...
    def delete_bank(self, bank_id: str) -> None: ...
    def close(self) -> None: ...


class RealHindsightGateway:
    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout_s = timeout_s
        # The sync Hindsight client binds an event loop to the thread that first uses it, so each
        # thread (API worker, job worker) gets its own client. Found by tests/test_hindsight_live.py.
        self._local = threading.local()
        self._all: list[Any] = []
        self._lock = threading.Lock()

    # -- plumbing ---------------------------------------------------------
    def _c(self):
        client = getattr(self._local, "client", None)
        if client is None:
            from hindsight_client import Hindsight

            kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": self.timeout_s}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            client = Hindsight(**kwargs)
            self._local.client = client
            with self._lock:
                self._all.append(client)
        return client

    def close(self) -> None:
        """Close every per-thread client (call from a thread that may close them, e.g. shutdown)."""
        with self._lock:
            clients, self._all = self._all, []
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - best-effort on shutdown
                log.warning("hindsight client close failed", exc_info=True)
        self._local = threading.local()

    def _wrap(self, op: str, fn):
        started = time.perf_counter()
        try:
            return fn()
        except HindsightError:
            raise
        except Exception as e:  # noqa: BLE001 - classify then re-raise typed
            raise self._classify(op, e) from e
        finally:
            log.info("hindsight.%s", op, extra={"duration_ms": int((time.perf_counter() - started) * 1000)})

    @staticmethod
    def _classify(op: str, e: Exception) -> HindsightError:
        status = getattr(e, "status", None) or getattr(e, "status_code", None)
        name = type(e).__name__
        # Connection-level / timeout failures
        if isinstance(e, (ConnectionError, TimeoutError, OSError)) or name in {
            "ClientConnectorError", "ClientConnectionError", "ServerDisconnectedError", "ClientOSError",
            "ServerTimeoutError", "ClientConnectorDNSError", "TimeoutError", "CancelledError",
        } or "Connect" in name or "Timeout" in name:
            return HindsightUnavailable(f"{op}: Hindsight unreachable ({name}: {str(e)[:200]})")
        if isinstance(status, int) and status >= 500:
            return HindsightUnavailable(f"{op}: Hindsight server error {status}")
        if status == 404 and re.search(r"bank\b.*\bnot found", str(e), re.I | re.S):
            return HindsightBankNotFound(f"{op}: bank not found on this Hindsight server")
        if isinstance(status, int):
            return HindsightError(f"{op}: Hindsight rejected the request ({status}): {str(e)[:200]}")
        return HindsightError(f"{op}: unexpected Hindsight failure ({name}: {str(e)[:200]})")

    # -- operations -------------------------------------------------------
    def health(self) -> HindsightHealth:
        try:
            v = self._wrap("get_version", lambda: self._c().get_version())
            return HindsightHealth(ok=True, version=getattr(v, "api_version", None))
        except HindsightError as e:
            return HindsightHealth(ok=False, detail=str(e))

    def ensure_bank(self, bank_id: str, name: str, mission: str) -> bool:
        """Create the bank if missing. Returns True if created. Idempotent."""
        validate_bank_id(bank_id)

        def _go() -> bool:
            c = self._c()
            try:
                c.get_bank_config(bank_id)  # raises 404 if absent in some versions; create_bank is upsert anyway
            except Exception:  # noqa: BLE001
                pass
            c.create_bank(bank_id, name=name, mission=mission, enable_observations=True)
            return True

        return self._wrap("create_bank", _go)

    def retain_document(
        self, bank_id: str, document_id: str, items: list[MemoryItemIn], replace: bool = True
    ) -> RetainOutcome:
        validate_bank_id(bank_id)
        if not items:
            raise HindsightError("retain_document called with no items")
        payload = []
        for it in items:
            d: dict[str, Any] = {"content": it.content}
            if it.context:
                d["context"] = it.context
            if it.tags:
                d["tags"] = it.tags
            if it.metadata:
                d["metadata"] = {k: str(v) for k, v in it.metadata.items()}
            if replace:
                d["update_mode"] = "replace"
            payload.append(d)
        started = time.perf_counter()
        resp = self._wrap(
            "retain_batch",
            lambda: self._c().retain_batch(bank_id, payload, document_id=document_id),
        )
        if not getattr(resp, "success", False):
            raise HindsightError(f"retain_batch reported failure: {resp}")
        usage = getattr(resp, "usage", None)
        return RetainOutcome(
            items_count=int(getattr(resp, "items_count", len(items)) or 0),
            document_id=document_id,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    @staticmethod
    def _to_memory(r: Any) -> RecalledMemory:
        return RecalledMemory(
            id=str(getattr(r, "id", "")),
            text=getattr(r, "text", "") or "",
            type=getattr(r, "type", None) or getattr(r, "fact_type", None),
            tags=list(getattr(r, "tags", None) or []),
            metadata=dict(getattr(r, "metadata", None) or {}),
            document_id=getattr(r, "document_id", None),
            context=getattr(r, "context", None),
        )

    def recall(
        self, bank_id: str, query: str, tags: list[str] | None = None, max_tokens: int = 1500, budget: str = "low"
    ) -> list[RecalledMemory]:
        validate_bank_id(bank_id)
        kwargs: dict[str, Any] = {"max_tokens": max_tokens, "budget": budget}
        if tags:
            kwargs["tags"] = tags
            kwargs["tags_match"] = "any_strict"
        try:
            resp = self._wrap("recall", lambda: self._c().recall(bank_id, query, **kwargs))
        except HindsightBankNotFound:
            return []  # nothing has been retained into this bank on this server yet: that is "no memories", not an outage
        return [self._to_memory(r) for r in (resp.results or [])]

    def list_document_memories(self, bank_id: str, search_query: str | None = None, limit: int = 50) -> list[RecalledMemory]:
        validate_bank_id(bank_id)
        try:
            resp = self._wrap(
                "list_memories", lambda: self._c().list_memories(bank_id, search_query=search_query, limit=limit)
            )
        except HindsightBankNotFound:
            return []
        out = []
        for m in resp.items or []:
            d = m if isinstance(m, dict) else m.model_dump()
            out.append(
                RecalledMemory(
                    id=str(d.get("id", "")),
                    text=d.get("text", ""),
                    type=d.get("fact_type"),
                    tags=list(d.get("tags") or []),
                    metadata=dict(d.get("metadata") or {}),
                    document_id=d.get("document_id"),
                    context=d.get("context"),
                )
            )
        return out

    def consolidate(self, bank_id: str, model_id: str, name: str, source_query: str) -> dict[str, Any]:
        """Create (first time) or refresh a Hindsight mental model that consolidates the bank's knowledge."""
        validate_bank_id(bank_id)
        c = self._c()

        def _go() -> dict[str, Any]:
            created = False
            try:
                c.get_mental_model(bank_id, model_id)
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) != 404:  # only "does not exist" means create; anything else is a real failure
                    raise
                r = c.create_mental_model(bank_id, name=name, source_query=source_query, id=model_id)
                created = True
                return {"mental_model_id": model_id, "created": True, "refreshed": False, "operation_id": getattr(r, "operation_id", None)}
            r = c.refresh_mental_model(bank_id, model_id)
            return {"mental_model_id": model_id, "created": created, "refreshed": True, "operation_id": getattr(r, "operation_id", None)}

        return self._wrap("mental_model", _go)

    def get_consolidated(self, bank_id: str, model_id: str) -> dict[str, Any] | None:
        """Hindsight's own consolidated view (AI-generated memory, NOT evidence). None if the model does not exist yet."""
        validate_bank_id(bank_id)

        def _go() -> dict[str, Any] | None:
            try:
                g = self._c().get_mental_model(bank_id, model_id, detail="full")
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) == 404:
                    return None
                raise
            d = g if isinstance(g, dict) else g.model_dump()
            return {k: d.get(k) for k in ("id", "name", "content", "is_stale", "last_refreshed_at", "last_refresh_failed_at", "source_query")}

        return self._wrap("get_mental_model", _go)

    def delete_bank(self, bank_id: str) -> None:
        validate_bank_id(bank_id)
        self._wrap("delete_bank", lambda: self._c().delete_bank(bank_id))


def build_gateway(settings) -> RealHindsightGateway:
    return RealHindsightGateway(settings.hindsight_base_url, settings.hindsight_api_key, settings.hindsight_timeout_s)
