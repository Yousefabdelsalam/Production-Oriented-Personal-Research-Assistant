"""Structured JSON logging with correlation ids and secret redaction.

Policy: log ids, stages, durations, counts, hashes and error classes — never API keys or document text.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
execution_id: contextvars.ContextVar[str] = contextvars.ContextVar("execution_id", default="-")
stage_name: contextvars.ContextVar[str] = contextvars.ContextVar("stage", default="-")

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password|authorization)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
]
SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie)")


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def sanitize_detail(obj, max_str: int = 600, depth: int = 0):
    """Make step/trace details safe to persist: redact secrets, truncate long strings, drop sensitive keys."""
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        return {str(k): ("[REDACTED]" if SENSITIVE_KEYS.search(str(k)) else sanitize_detail(v, max_str, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        items = list(obj)
        out = [sanitize_detail(v, max_str, depth + 1) for v in items[:200]]
        if len(items) > 200:
            out.append(f"…(+{len(items) - 200} more)")
        return out
    if isinstance(obj, str):
        s = redact(obj)
        return s if len(s) <= max_str else s[: max_str - 1] + "…"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return redact(str(obj))[:max_str]


class JsonFormatter(logging.Formatter):
    _STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "correlation_id": correlation_id.get(),
            "execution_id": execution_id.get(),
            "stage": stage_name.get(),
        }
        for k, v in record.__dict__.items():
            if k not in self._STD and not k.startswith("_"):
                payload[k] = sanitize_detail(v, 300)
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["error"] = redact(str(record.exc_info[1]))[:500]
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_pra", False) for h in root.handlers):
        root.setLevel(level)
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    h._pra = True  # type: ignore[attr-defined]
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
