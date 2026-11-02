"""Phase 2 vertical slice: workspace -> ingest PDF -> evidence -> memory -> ask -> cited answer.

Offline variant uses InMemoryHindsightDouble (logic only). The `hindsight`-marked variant runs the SAME flow
against the real Hindsight server and is the verification of the real integration.
"""
from __future__ import annotations

import pytest

from app.models import EvidencePassage, ExtractedFinding, Source, SourceVersion, WorkflowExecution
from app.services.workspaces import create_workspace
from evaldata import corpus
from tests.conftest import new_live_workspace
from tests.helpers import drain, get_execution, ingest_pdf


def ask(services, ws_id, question, conversation_id=None):
    ex = services.runner.start_and_run("qa", state=__import__("app.harness.workflows.qa", fromlist=["QAState"]).QAState(
        workspace_id=ws_id, question=question, conversation_id=conversation_id), workspace_id=ws_id, trigger={"type": "question"})
    return ex


def test_ingest_pdf_creates_cited_evidence_and_memory(services, ws, double):
    res = ingest_pdf(services, ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    assert not res.duplicate
    with services.session() as s:
        src = s.get(Source, res.source_id)
        assert src.status == "completed", src.error
        assert src.current_version == 1
        ver = s.query(SourceVersion).filter_by(source_id=src.id).one()
        assert "reduces hallucination by 31%" in ver.extracted_text  # original text preserved
        passages = s.query(EvidencePassage).filter_by(source_id=src.id).all()
        assert passages and {p.page_start for p in passages} == {1, 2}  # page references preserved
        # every passage is an exact substring of the stored text (so quotes/offsets are reliable)
        assert all(ver.extracted_text[p.char_start:p.char_end] == p.text for p in passages)
        findings = s.query(ExtractedFinding).filter_by(source_id=src.id, validated=True).all()
        assert any("31%" in f.statement for f in findings)
        assert all(f.quote and f.passage_id for f in findings)
        assert {f.memory_state for f in findings} <= {"retained", "skipped"}
    bank = [b for b in double.banks][0]
    texts = double.all_texts(bank)
    assert any("31%" in t for t in texts)
    assert all("Source states" in t or "Inference" in t or "Uncertain" in t or "The user wrote" in t for t in texts)  # epistemic labels


def test_ask_returns_verifiable_citations(services, ws):
    ingest_pdf(services, ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    ex = ask(services, ws, "How much does MemoryBank-Lite reduce hallucination on LongQA?")
    assert ex.status == "succeeded", ex.error
    ans = ex.result["answer"]
    assert ans["mode"] == "supported", ans
    assert ans["generation_mode"] == "extractive"
    claim = next(c for c in ans["claims"] if "31%" in c["text"])
    cit = claim["citations"][0]
    assert cit["verified"] and cit["page"] == 1
    with services.session() as s:  # citation resolves to a real stored passage containing the quote
        p = s.get(EvidencePassage, cit["passage_id"])
        assert p is not None and cit["quote"] in p.text
        assert s.get(Source, cit["source_id"]).title.startswith("MemoryBank-Lite")


def test_insufficient_evidence_is_explicit(services, ws):
    ingest_pdf(services, ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    ans = ask(services, ws, "What is the boiling point of tungsten?").result["answer"]
    assert ans["mode"] == "insufficient" and ans["claims"] == []
    assert "not" in ans["answer"].lower()


@pytest.mark.hindsight
def test_live_end_to_end_with_real_hindsight(live_services):
    """The real thing: real Hindsight stores the findings and recalls them during the question."""
    ws_id = new_live_workspace(live_services)
    res = ingest_pdf(live_services, ws_id, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    with live_services.session() as s:
        src = s.get(Source, res.source_id)
        assert src.status == "completed", src.error
    ex = get_execution(live_services, res.execution_id)
    mem = next(st for st in ex.steps if st.stage == "RETAIN_USEFUL_MEMORY" and st.status == "succeeded")
    assert mem.detail["memory"]["retained"] >= 1 and not mem.detail["memory"]["idempotent_skip"]

    gw = live_services.gateway
    with live_services.session() as s:
        bank = s.query(__import__("app.models", fromlist=["Workspace"]).Workspace).get(ws_id).bank_id
    mems = gw.recall(bank, "hallucination reduction LongQA")
    assert any("31%" in m.text for m in mems), [m.text for m in mems]

    qa = ask(live_services, ws_id, "How much does MemoryBank-Lite reduce hallucination on LongQA?")
    assert qa.status == "succeeded", qa.error
    ans = qa.result["answer"]
    assert ans["mode"] == "supported"
    assert ans["memory_used"], "real Hindsight recall should have contributed memory context"
    step = next(st for st in get_execution(live_services, qa.id).steps if st.stage == "RECALL_HINDSIGHT_MEMORY")
    assert step.detail["memory"]["returned"] >= 1
