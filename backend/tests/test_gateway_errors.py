"""Hindsight error classification without a server: the exception shapes below are the real ones the SDK raised."""
from __future__ import annotations

import pytest

from app.services.hindsight_gateway import (
    HindsightBankNotFound, HindsightError, HindsightUnavailable, RealHindsightGateway,
)


class _ApiError(Exception):
    """Same attributes as hindsight_client_api.exceptions.NotFoundException / ServiceException."""

    def __init__(self, status, body):
        self.status = status
        super().__init__(f"({status})\nReason: x\nHTTP response body: {body}")


class _Resp:
    results: list = []
    items: list = []


class _Client:
    def __init__(self, exc=None):
        self.exc = exc

    def recall(self, *a, **k):
        if self.exc:
            raise self.exc
        return _Resp()

    list_memories = recall


def _gw(exc):
    g = RealHindsightGateway("http://unused.invalid")
    g._c = lambda: _Client(exc)  # type: ignore[method-assign]
    return g


MISSING = _ApiError(404, '{"detail":"Bank \'pra-ai-recourses\' not found"}')


def test_missing_bank_is_classified_as_such():
    assert isinstance(RealHindsightGateway._classify("recall", MISSING), HindsightBankNotFound)


def test_recall_and_list_on_a_missing_bank_return_no_memories():
    g = _gw(MISSING)
    assert g.recall("pra-ai-recourses", "what is ML?") == []
    assert g.list_document_memories("pra-ai-recourses") == []


@pytest.mark.parametrize("exc,expected", [
    (_ApiError(404, '{"detail":"Not Found"}'), HindsightError),                       # wrong URL/route is NOT 'no memories'
    (_ApiError(400, '{"detail":"bad"}'), HindsightError),
    (_ApiError(500, '{"detail":"Fact extraction failed"}'), HindsightUnavailable),
    (ConnectionError("refused"), HindsightUnavailable),
])
def test_real_failures_are_still_reported(exc, expected):
    g = _gw(exc)
    with pytest.raises(expected) as ei:
        g.recall("pra-ai-recourses", "q")
    assert not isinstance(ei.value, HindsightBankNotFound)


def test_qa_answers_without_a_memory_warning_when_the_bank_is_missing(services, ws):
    """End to end through the QA workflow with the real gateway logic: no 'Hindsight memory was unavailable' warning."""
    from tests.helpers import ingest_note
    from tests.test_vertical_slice import ask

    ingest_note(services, ws, "Mastering vectorization in NumPy is critical for writing efficient ML code before GPU acceleration.", "ML notes")
    services.gateway = _gw(MISSING)  # a real RealHindsightGateway whose server says 'bank not found'
    ex = ask(services, ws, "what is ML foundations?")
    assert ex.status == "succeeded"
    ans = ex.result["answer"]
    assert not any("unavailable" in w for w in ans["warnings"]), ans["warnings"]
    assert ans["claims"], "still answers from the sources"
