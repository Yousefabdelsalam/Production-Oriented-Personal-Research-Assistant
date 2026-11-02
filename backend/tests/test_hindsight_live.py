"""LIVE integration tests against a real Hindsight server. No mocks here.

Skipped (loudly, with the reason) when the server at HINDSIGHT_BASE_URL is unreachable.
Each test uses a throwaway bank prefixed `pra-test-` and deletes it afterwards.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest

from app.config import get_settings
from app.services.hindsight_gateway import (
    HindsightError,
    HindsightUnavailable,
    MemoryItemIn,
    RealHindsightGateway,
    build_gateway,
)

pytestmark = pytest.mark.hindsight


@pytest.fixture(scope="module")
def gw():
    g = build_gateway(get_settings())
    h = g.health()
    if not h.ok:
        pytest.skip(f"LIVE Hindsight NOT reachable at {g.base_url}: {h.detail}. Start it (docs/setup.md) to run these tests.")
    yield g
    g.close()


@pytest.fixture
def bank(gw):
    bank_id = "pra-test-" + uuid.uuid4().hex[:8]
    gw.ensure_bank(bank_id, "pytest bank", "Throwaway bank for integration tests.")
    yield bank_id
    try:
        gw.delete_bank(bank_id)
    except HindsightError:
        pass


def test_recall_on_a_bank_that_does_not_exist_is_empty_not_an_outage(gw):
    """A brand-new Hindsight server has no bank for an existing workspace: that is 'no memories yet', not 'unavailable'.
    (Seen by the user as 'Hindsight memory was unavailable ... (AppError)' after moving to a fresh instance.)"""
    missing = "pra-test-missing-" + uuid.uuid4().hex[:8]
    assert gw.recall(missing, "anything at all", max_tokens=200) == []
    assert gw.list_document_memories(missing) == []
    assert missing not in [b for b in _bank_ids(gw)]  # and the read did not create the bank as a side effect


def _bank_ids(gw):
    import json
    import urllib.request

    raw = json.load(urllib.request.urlopen(gw.base_url + "/v1/default/banks", timeout=15))
    return [b.get("bank_id") or b.get("id") for b in (raw["banks"] if isinstance(raw, dict) else raw)]


def test_health_reports_version(gw):
    h = gw.health()
    assert h.ok and h.version, h


def test_ensure_bank_is_idempotent(gw):
    bank_id = "pra-test-" + uuid.uuid4().hex[:8]
    try:
        gw.ensure_bank(bank_id, "t", "m")
        gw.ensure_bank(bank_id, "t", "m")  # must not raise
    finally:
        gw.delete_bank(bank_id)


def test_retain_then_recall_with_tag_filter_and_provenance(gw, bank):
    out = gw.retain_document(
        bank,
        "src_alpha",
        [
            MemoryItemIn(
                content="Alpha Paper (source src_alpha, page 4) reports that retrieval-augmented generation reduces hallucination by 30% on open-domain QA.",
                context="research finding; origin=source_stated",
                tags=["source:src_alpha", "kind:finding"],
                metadata={"source_id": "src_alpha", "finding_id": "fnd_1", "page": "4"},
            ),
            MemoryItemIn(
                content="Alpha Paper (source src_alpha, page 9) notes that its evaluation only covers English benchmarks.",
                context="research limitation; origin=source_stated",
                tags=["source:src_alpha", "kind:limitation"],
                metadata={"source_id": "src_alpha", "finding_id": "fnd_2", "page": "9"},
            ),
        ],
    )
    assert out.items_count >= 1 and out.document_id == "src_alpha"

    mems = gw.recall(bank, "How much does RAG reduce hallucination?", tags=["source:src_alpha"])
    assert mems, "recall returned nothing after a successful retain"
    top = mems[0]
    assert "src_alpha" in top.source_ids()
    assert top.document_id == "src_alpha"
    assert top.metadata.get("source_id") == "src_alpha"  # provenance metadata round-trips

    # tag filter excludes other sources
    assert gw.recall(bank, "hallucination", tags=["source:src_other"]) == []


def test_replacing_a_document_does_not_duplicate_or_keep_stale_memory(gw, bank):
    gw.retain_document(bank, "note_1", [MemoryItemIn(
        content="Note: I believe vector databases always beat BM25 for scientific search.",
        tags=["source:note_1", "kind:user_note"], metadata={"source_id": "note_1"})])
    before = [m.text for m in gw.list_document_memories(bank)]
    assert before, "nothing stored"

    gw.retain_document(bank, "note_1", [MemoryItemIn(
        content="Note: I now think hybrid BM25 plus vectors is best for scientific search.",
        tags=["source:note_1", "kind:user_note"], metadata={"source_id": "note_1"})])
    after = [m.text for m in gw.list_document_memories(bank)]
    joined = " ".join(after).lower()
    assert "hybrid" in joined
    assert "always beat" not in joined, f"stale memory survived replace: {after}"


def test_identical_retry_is_cheap_and_does_not_add_memories(gw, bank):
    item = MemoryItemIn(content="Beta Paper (source src_beta) introduces the Zephyr indexing method.",
                        tags=["source:src_beta"], metadata={"source_id": "src_beta"})
    gw.retain_document(bank, "src_beta", [item])
    n1 = len(gw.list_document_memories(bank))
    t = time.perf_counter()
    gw.retain_document(bank, "src_beta", [item])
    n2 = len(gw.list_document_memories(bank))
    assert n1 == n2
    assert time.perf_counter() - t < 5, "identical retry should be a fast no-op on the server"


def test_works_from_a_worker_thread(gw, bank):
    result: dict = {}

    def run():
        try:
            result["mems"] = gw.recall(bank, "anything")
        except Exception as e:  # noqa: BLE001
            result["err"] = e

    th = threading.Thread(target=run)
    th.start()
    th.join(60)
    assert "err" not in result, result.get("err")


def test_unreachable_server_maps_to_unavailable():
    g = RealHindsightGateway("http://127.0.0.1:9", timeout_s=3)  # closed port: real connection failure
    try:
        h = g.health()
        assert h.ok is False
        with pytest.raises(HindsightUnavailable):
            g.recall("pra-valid-bank", "q")
    finally:
        g.close()


def test_invalid_bank_id_rejected_before_network():
    g = RealHindsightGateway("http://127.0.0.1:9", timeout_s=1)
    with pytest.raises(HindsightError) as ei:
        g.recall("Bad Bank/Id", "q")
    assert not isinstance(ei.value, HindsightUnavailable)
