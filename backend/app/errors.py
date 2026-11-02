"""Typed errors the harness uses to decide whether to retry, fail the stage, or reject the input."""
from __future__ import annotations


class AppError(Exception):
    code = "app_error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class InvalidInput(AppError):
    """The submission itself is unacceptable (wrong type, too big, malformed). Never retried."""
    code = "invalid_input"


class ExtractionError(AppError):
    """Content could not be extracted (corrupt/scanned PDF, unreadable page...). Not retried automatically."""
    code = "extraction_failed"


class FetchBlocked(InvalidInput):
    """URL refused by SSRF / policy rules."""
    code = "url_blocked"


class FetchError(AppError):
    """Network/HTTP failure fetching a URL; transient ones are retryable."""
    code = "fetch_failed"


class RetryableError(AppError):
    """Transient dependency failure (Hindsight/LLM outage, timeout). The runner retries with backoff."""
    code = "transient"
    retryable = True


class ValidationFailed(AppError):
    """A harness validation gate rejected an output; the stage fails without retry."""
    code = "validation_failed"


class PolicyViolation(AppError):
    code = "policy_violation"


class IllegalTransition(AppError):
    code = "illegal_transition"


class NotFound(AppError):
    code = "not_found"
