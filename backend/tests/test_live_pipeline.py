"""LIVE pipeline tests against the real Hindsight server (marker: hindsight). No test doubles for Hindsight here.
Slow (each retain runs a server-side LLM extraction, ~10-15 s) — they are the evidence that the real integration works.
"""
from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.harness.workflows.memory import MODEL_ID, MemoryState
from app.models import MemoryOperation, Source, Workspace
from app.services import sources as src_svc
from app.services.hindsight_gateway import RealHindsightGateway, build_gateway
from app.services.llm import ModelRuntime, NullEmbedder
from app.wiring import build_services
from evaldata import corpus
from tests.conftest import make_settings, new_live_workspace
from tests.helpers import drain, get_execution, ingest_pdf, make_pdf
from tests.test_vertical_slice import ask

pytestmark = pytest.mark.hindsight


def test_live_multi_source_synthesis_memory_and_later_session_recall(live_services, tmp_path):
    svc = live_services
    svc.settings.auto_synthesize = True
    ws_id = new_live_workspace(svc, "Live Multi")
    for p in (corpus.PAPER_A, corpus.PAPER_C):
        r = ingest_pdf(svc, ws_id, p["filename"], p["pages"])
        with svc.session() as s:
            assert s.get(Source, r.source_id).status == "completed"
    with svc.session() as s:
        bank = s.get(Workspace, ws_id).bank_id
        ops = s.query(MemoryOperation).filter_by(workspace_id=ws_id, op="retain", status="done").all()
        assert len(ops) >= 3  # 2 source retains + >=1 relationship retain (contradiction), each ledgered
    # relationship memory really exists in Hindsight, labelled as an unverified inference
    mems = svc.gateway.recall(bank, "do MemoryBank-Lite and the critical evaluation contradict each other?")
    assert any("Unverified hypothesis" in m.text or "contradict" in m.text.lower() for m in mems), [m.text[:120] for m in mems]

    # ---- "later session": brand-new services/engine/gateway objects over the same DB file and the same Hindsight server
    later_settings = Settings(**{**svc.settings.model_dump(), "_env_file": None})
    later = build_services(later_settings, gateway=build_gateway(later_settings), models=ModelRuntime(None, NullEmbedder()), migrate=False)
    try:
        ex = later.runner.start_and_run("qa", state=__import__("app.harness.workflows.qa", fromlist=["QAState"]).QAState(
            workspace_id=ws_id, question="Does memory consolidation reduce hallucination on LongQA?"), workspace_id=ws_id)
        assert ex.status == "succeeded", ex.error
        ans = ex.result["answer"]
        assert ans["mode"] == "conflicting"
        assert ans["memory_used"], "recall from the real Hindsight bank should supply memory in a fresh session"
        assert any("31%" in m["text"] or "reproduc" in m["text"] or "significantly" in m["text"] for m in ans["memory_used"])
        step = next(s for s in get_execution(later, ex.id).steps if s.stage == "RECALL_HINDSIGHT_MEMORY")
        assert step.detail["memory"]["returned"] >= 1
    finally:
        later.gateway.close()
        later.session_factory.kw["bind"].dispose()

    # ---- consolidation through the real mental-model API
    eid = svc.runner.create("memory", state=MemoryState(workspace_id=ws_id, reason="live test"), workspace_id=ws_id, idempotency_key=f"live-{ws_id}")
    drain(svc)
    ex = get_execution(svc, eid)
    assert ex.status == "succeeded", ex.error
    assert ex.result["consolidation"]["mental_model_id"] == MODEL_ID
    view = None
    for _ in range(30):  # refresh is asynchronous on the Hindsight side
        view = svc.gateway.get_consolidated(bank, MODEL_ID)
        if view and view.get("content"):
            break
        time.sleep(5)
    assert view and view["content"], "Hindsight did not produce a consolidated view in time"
    assert "31%" in view["content"] or "hallucination" in view["content"].lower()


def test_live_real_outage_then_real_recovery_via_resume(tmp_path):
    """First attempt talks to a closed port (a real connection failure through the real client); then the real server is used."""
    real = build_gateway(make_settings(tmp_path))
    if not real.health().ok:
        pytest.skip("LIVE Hindsight NOT reachable")
    dead = RealHindsightGateway("http://127.0.0.1:9", timeout_s=2)
    svc = build_services(make_settings(tmp_path), gateway=dead, models=ModelRuntime(None, NullEmbedder()))
    svc.sleep = lambda s: None
    ws_id = new_live_workspace(type("L", (), {"session": svc.session, "settings": svc.settings, "extras": {"created_banks": []}})(), "Live Outage")
    try:
        res = ingest_pdf(svc, ws_id, corpus.PAPER_B["filename"], corpus.PAPER_B["pages"])
        ex = get_execution(svc, res.execution_id)
        assert ex.status == "failed" and ex.failed_stage == "RETAIN_USEFUL_MEMORY"
        retries = [s for s in ex.steps if s.stage == "RETAIN_USEFUL_MEMORY"]
        assert len(retries) == 3 and all(s.detail["error_code"] == "hindsight_unavailable" for s in retries)  # 1 + 2 bounded retries
        with svc.session() as s:
            src = s.get(Source, res.source_id)
            assert src.status == "partial"

        svc.gateway = real  # server reachable again
        assert src_svc.retry_source(svc, res.source_id) == res.execution_id
        drain(svc)
        ex2 = get_execution(svc, res.execution_id)
        assert ex2.status == "succeeded" and ex2.resume_count == 1
        with svc.session() as s:
            assert s.get(Source, res.source_id).status == "completed"
            bank = s.get(Workspace, ws_id).bank_id
        assert real.recall(bank, "hybrid retrieval recall")
    finally:
        try:
            with svc.session() as s:
                real.delete_bank(s.get(Workspace, ws_id).bank_id)
        except Exception:  # noqa: BLE001
            pass
        real.close()
        dead.close()
        svc.session_factory.kw["bind"].dispose()
