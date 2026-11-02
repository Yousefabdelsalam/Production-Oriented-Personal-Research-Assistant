"""Living summary: versioning, validation gates, no-op detection, preservation, rollback; memory consolidation workflow."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.harness.workflows.memory import MemoryState
from app.harness.workflows.summary import SummaryState
from app.main import create_app
from app.models import EvidencePassage, ExtractedFinding, MemoryOperation, Source, SummaryVersion, Workspace
from app.services import summary as sm
from app.services.llm import ModelRuntime, NullEmbedder
from app.services.workspaces import create_workspace
from app.wiring import build_services
from evaldata import corpus
from tests.conftest import make_settings
from tests.doubles import FlakyGateway, InMemoryHindsightDouble, ScriptedLLM
from tests.helpers import drain, get_execution, ingest_note, ingest_pdf


@pytest.fixture
def sv(tmp_path):
    double = InMemoryHindsightDouble()
    svc = build_services(make_settings(tmp_path, auto_synthesize=True), gateway=double, models=ModelRuntime(None, NullEmbedder()))
    svc.sleep = lambda s: None
    svc.double = double
    with svc.session() as s:
        svc.ws = create_workspace(s, svc.settings, "Agentic AI").id
    yield svc
    svc.session_factory.kw["bind"].dispose()


def update(svc, **kw):
    eid = svc.runner.create("summary", state=SummaryState(workspace_id=svc.ws, **kw), workspace_id=svc.ws,
                            idempotency_key=f"t-{len(kw)}-{svc.runner.defs and id(object())}")
    svc.runner.claim(eid)
    return svc.runner.run(eid)


def versions(svc):
    with svc.session() as s:
        rows = s.query(SummaryVersion).filter_by(workspace_id=svc.ws).order_by(SummaryVersion.version).all()
        for r in rows:
            s.expunge(r)
        return rows


def content(v):
    return sm.parse_content(v.content)


def test_per_update_cap_defers_items_visibly_and_converges_without_losing_any(sv, monkeypatch):
    for p in (corpus.PAPER_A, corpus.PAPER_B, corpus.PAPER_C):
        ingest_pdf(sv, sv.ws, p["filename"], p["pages"])
    with sv.session() as s:
        uncapped = len(sm.build_items(s, sv.ws)[0])
    monkeypatch.setattr(sm, "MAX_NEW_PER_SECTION", 2)
    first = update(sv)
    assert first.result["outcome"] == "created" and first.result["deferred_items"] > 0
    (v1,) = versions(sv)
    assert "deferred by the per-update cap" in v1.change["summary"]  # the user is told the summary is not complete yet
    seen = [first.result["deferred_items"]]
    for _ in range(uncapped):  # bounded: each update adds >= 1 item or is a no-op
        ex = update(sv)
        if ex.result["outcome"] == "no_change":
            break
        seen.append(ex.result["deferred_items"])
    else:
        pytest.fail("deferred backlog never drained")
    assert seen == sorted(seen, reverse=True) and seen[-1] == 0  # backlog only shrinks and ends empty
    final = content(versions(sv)[-1])
    assert len(final.all_items()) == uncapped  # nothing was lost to the cap
    n = len(versions(sv))
    assert update(sv).result["outcome"] == "no_change" and len(versions(sv)) == n  # and now the no-op rule holds


@pytest.mark.parametrize("name", ["Agentic AI 2025", "LLMOps v2.0 notes (Q3 2026)", "eval-5b0472ab-727"])
def test_workspace_names_containing_numbers_do_not_break_the_summary(sv, name):
    """The deterministic overview quotes the workspace name; its digits are not 'invented figures'.
    (Found when the evaluation sandbox's random label '…04…727' made every summary fail validation.)"""
    from app.models import Workspace

    with sv.session() as s:
        s.get(Workspace, sv.ws).name = name
    for p in (corpus.PAPER_A, corpus.PAPER_B):
        ingest_pdf(sv, sv.ws, p["filename"], p["pages"])
    ex = update(sv)
    assert ex.status == "succeeded" and ex.result["outcome"] == "created", ex.error
    assert name in content(versions(sv)[0]).overview


def test_overview_still_rejects_figures_that_are_not_in_the_summary(sv):
    for p in (corpus.PAPER_A, corpus.PAPER_B):
        ingest_pdf(sv, sv.ws, p["filename"], p["pages"])
    ok = update(sv)
    assert ok.status == "succeeded"
    with sv.session() as s:
        from app.models import SummaryVersion, Workspace

        cur = sm.parse_content(s.query(SummaryVersion).filter_by(workspace_id=sv.ws, version=1).one().content)
        bad = cur.model_copy(update={"overview": cur.overview + " Accuracy improved by 9137 points."})
        rep = sm.validate_content(s, sv.ws, bad, cur)
        assert not rep.ok and any("figures not found" in p and "9137" in p for p in rep.problems)


def _paraphrase_note_finding(sv, new_statement):
    from app.models import ExtractedFinding

    with sv.session() as s:
        f = s.query(ExtractedFinding).filter_by(workspace_id=sv.ws, epistemic="user_note").first()
        f.statement = new_statement
        return f.quote


def test_faithful_llm_paraphrase_of_a_user_note_is_not_rejected_because_of_the_template_label(sv):
    """Found with the real gpt-5-mini: 'I suspect chunk size matters more…' -> 'The author suspects chunk size has a greater effect…'.
    The finding passed validation at ingestion; the summary used to score it WITH its '(your note) ' label and fail the whole update."""
    from tests.helpers import ingest_note

    ingest_note(sv, sv.ws, "I suspect chunk size matters more than the choice of embedding model.", "My view on hybrid search")
    quote = _paraphrase_note_finding(sv, "The author suspects that chunk size has a greater effect on performance than the choice of embedding model.")
    assert sm.support_score(sm.strip_template("(your note) The author suspects that chunk size has a greater effect on performance than the choice of embedding model."), [quote + " My view on hybrid search"]).score >= sm.SUPPORT_THRESHOLD
    ex = update(sv)
    assert ex.status == "succeeded" and ex.result["outcome"] == "created", ex.error
    assert any(i.text.startswith("(your note) The author suspects") for i in content(versions(sv)[0]).all_items())


def test_unsupported_paraphrase_is_still_rejected_and_last_valid_summary_kept(sv):
    from tests.helpers import ingest_note

    ingest_note(sv, sv.ws, "I suspect chunk size matters more than the choice of embedding model.", "My view on hybrid search")
    assert update(sv).status == "succeeded"
    before = len(versions(sv))
    ingest_note(sv, sv.ws, "Reranking with a cross-encoder improves precision at the top of the result list.", "Rerank note")
    _paraphrase_note_finding(sv, "The author proves that quantum annealing guarantees perfect retrieval for every dataset.")
    ex = update(sv)
    assert ex.status == "failed" and ex.failed_stage == "VALIDATE_PROPOSAL" and "not supported by its citations" in ex.error
    assert len(versions(sv)) == before  # nothing partial was persisted


def test_first_summary_is_created_with_all_required_sections_and_valid_citations(sv):
    for p in (corpus.PAPER_A, corpus.PAPER_B, corpus.PAPER_C):
        ingest_pdf(sv, sv.ws, p["filename"], p["pages"])
    ex = update(sv)
    assert ex.status == "succeeded" and ex.result["outcome"] == "created" and ex.result["created_version"] == 1
    (v1,) = versions(sv)
    c = content(v1)
    assert set(c.sections) >= {k for k, _ in sm.SECTIONS}
    assert c.sections["core_concepts"] and c.sections["major_findings"] and c.sections["methods"] and c.sections["limitations"]
    assert c.sections["contradictions"], "the A-vs-C conflict should appear under contradictions"
    assert all(i.status_hint == "hypothesis" for i in c.sections["contradictions"])  # heuristic links are not presented as facts
    for h in ("## Core concepts and definitions", "## Major findings", "## Contradictions and competing explanations",
              "## Limitations and open research questions", "## Supporting sources"):
        assert h in v1.markdown
    assert "[NEW]" in v1.markdown and "[HYPOTHESIS]" in v1.markdown
    with sv.session() as s:  # every citation resolves to a real passage containing the verbatim quote
        for it in c.all_items():
            for cit in it.citations:
                assert cit.quote in s.get(EvidencePassage, cit.passage_id).text
    assert len(c.source_ids) == 3 and v1.parent_version is None


def test_new_evidence_creates_new_version_preserving_existing_knowledge(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    ingest_pdf(sv, sv.ws, corpus.PAPER_B["filename"], corpus.PAPER_B["pages"])
    update(sv)
    v1 = versions(sv)[0]
    k1 = {i.key for i in content(v1).all_items()}
    d = ingest_pdf(sv, sv.ws, corpus.PAPER_D["filename"], corpus.PAPER_D["pages"])
    ex = update(sv)
    assert ex.result["outcome"] == "created" and ex.result["created_version"] == 2
    v2 = versions(sv)[1]
    c2 = content(v2)
    k2 = {i.key for i in c2.all_items()}
    assert k1 <= k2, "existing items must be preserved"
    added = [i for i in c2.all_items() if i.key not in k1]
    assert added and all(d.source_id in i.source_ids for i in added)
    assert any("15%" in i.text for i in added)
    # status semantics: old items are ESTABLISHED, new ones NEW
    statuses = {i.key: sm.item_status(i, 2) for i in c2.all_items()}
    assert all(statuses[k] == "established" for k in k1) and all(statuses[i.key] == "new" for i in added)
    assert v2.parent_version == 1 and d.source_id in v2.change["new_source_ids"]
    assert "New developments in this version" in v2.markdown and "15%" in v2.markdown.split("New developments in this version")[1]
    assert len(v2.change["added"]) == len(added) and v2.change["removed"] == []
    # v1 is still fully accessible and unchanged
    assert versions(sv)[0].content == v1.content


def test_source_without_findings_does_not_change_the_summary(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    update(sv)
    ingest_note(sv, sv.ws, corpus.NOTE_LOGISTICS["text"], corpus.NOTE_LOGISTICS["title"])
    ex = update(sv)
    assert ex.status == "succeeded" and ex.result["outcome"] == "no_change" and ex.result["created_version"] is None
    assert len(versions(sv)) == 1
    with sv.session() as s:
        assert s.get(Workspace, sv.ws).current_summary_version == 1
    step = next(s for s in get_execution(sv, ex.id).steps if s.stage == "COMPARE_WITH_CURRENT")
    assert "no meaningful change" in step.detail["decision"]
    assert [s.stage for s in get_execution(sv, ex.id).steps][-1] == "DONE"
    assert "PERSIST_VERSION" not in [s.stage for s in get_execution(sv, ex.id).steps]


def test_repeated_update_without_new_information_is_a_noop(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    update(sv)
    ex = update(sv)
    assert ex.result["outcome"] == "no_change" and len(versions(sv)) == 1


def test_empty_workspace_creates_no_version(sv):
    ex = update(sv)
    assert ex.status == "succeeded" and ex.result["outcome"] == "no_change" and versions(sv) == []


def test_validation_failure_preserves_last_valid_summary_tampered_evidence(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    update(sv)
    before = versions(sv)[0]
    ingest_pdf(sv, sv.ws, corpus.PAPER_D["filename"], corpus.PAPER_D["pages"])
    with sv.session() as s:  # corrupt a quote so a citation can no longer be verified
        f = next(f for f in s.query(ExtractedFinding).filter_by(validated=True).all() if "15%" in f.statement)
        f.quote = "Reflection loops improve task success rate by 95% on the ToolBench benchmark."
    ex = update(sv)
    assert ex.status == "failed" and ex.failed_stage == "VALIDATE_PROPOSAL" and "verbatim" in ex.error
    after = versions(sv)
    assert len(after) == 1 and after[0].content == before.content and after[0].markdown == before.markdown
    with sv.session() as s:
        assert s.get(Workspace, sv.ws).current_summary_version == 1
    step = [s for s in get_execution(sv, ex.id).steps if s.stage == "VALIDATE_PROPOSAL"][-1]
    assert step.status == "failed" and step.detail["validations"][0]["passed"] is False  # failure details are inspectable
    assert any("verbatim" in p for p in step.detail["validations"][0]["info"]["problems"])


def test_validation_gate_rejects_silently_dropped_knowledge(sv, monkeypatch):
    for p in (corpus.PAPER_A, corpus.PAPER_B):
        ingest_pdf(sv, sv.ws, p["filename"], p["pages"])
    update(sv)
    ingest_pdf(sv, sv.ws, corpus.PAPER_D["filename"], corpus.PAPER_D["pages"])
    real = sm.assemble

    def lossy(items, stats, prev, valid_prev, ws_name, **kw):  # a buggy/greedy proposer that forgets old items
        return real(items, stats, None, set(), ws_name, **kw).model_copy(update={}) if False else _drop_first(real(items, stats, prev, valid_prev, ws_name, **kw))

    def _drop_first(c):
        sec = next(k for k, v in c.sections.items() if v)
        c.sections[sec] = c.sections[sec][1:]
        return c

    monkeypatch.setattr(sm, "assemble", lossy)
    ex = update(sv)
    assert ex.status == "failed" and "dropped without justification" in ex.error
    assert len(versions(sv)) == 1


def test_llm_overview_with_invented_figure_falls_back_to_deterministic(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    sv.models = ModelRuntime(ScriptedLLM(lambda p, u: {"overview": "Agents improve accuracy by 99% according to every source."}), NullEmbedder())
    ex = update(sv)
    assert ex.status == "succeeded"
    c = content(versions(sv)[0])
    assert c.overview_mode == "deterministic" and "99%" not in c.overview
    assert any("LLM overview failed validation" in w for w in ex.result["warnings"])


def test_valid_llm_overview_is_accepted_and_labelled(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    sv.models = ModelRuntime(ScriptedLLM(lambda p, u: {"overview": "This workspace covers hierarchical memory consolidation for LLM agents and reports a 31% hallucination reduction on LongQA."}), NullEmbedder())
    ex = update(sv)
    c = content(versions(sv)[0])
    assert ex.status == "succeeded" and c.overview_mode == "llm" and "31%" in c.overview and ex.model_calls == 1


def test_superseded_source_version_removes_item_with_recorded_reason_but_keeps_the_rest(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    n = ingest_note(sv, sv.ws, "I believe pure dense retrieval is enough for scientific papers and BM25 adds little.", "Retrieval view")
    update(sv)
    v1 = versions(sv)[0]
    assert any("dense retrieval" in i.text for i in content(v1).all_items())
    ingest_note(sv, sv.ws, "I now think hybrid retrieval with BM25 is clearly better for scientific papers than dense alone.", "Retrieval view", source_id=n.source_id)
    ex = update(sv)
    assert ex.result["outcome"] == "created"
    v2 = versions(sv)[1]
    removed = v2.change["removed"]
    assert removed and all("superseded" in r["reason"] for r in removed) and any("dense retrieval" in r["text"] for r in removed)
    assert any("hybrid retrieval" in i.text for i in content(v2).all_items())
    paper_a_keys = {i.key for i in content(v1).all_items() if "src" in str(i.source_ids) and n.source_id not in i.source_ids}
    assert paper_a_keys <= {i.key for i in content(v2).all_items()}  # unrelated knowledge untouched


def test_rollback_creates_new_version_and_keeps_history(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    update(sv)
    ingest_pdf(sv, sv.ws, corpus.PAPER_D["filename"], corpus.PAPER_D["pages"])
    update(sv)
    v1, v2 = versions(sv)
    ex = update(sv, action="rollback", rollback_to=1)
    assert ex.status == "succeeded" and ex.result["created_version"] == 3
    vs = versions(sv)
    assert [v.version for v in vs] == [1, 2, 3] and vs[2].kind == "rollback" and vs[2].parent_version == 2
    assert {i.key for i in content(vs[2]).all_items()} == {i.key for i in content(v1).all_items()}
    assert "Rolled back to version 1" in vs[2].change["summary"]
    assert vs[1].content == v2.content  # history is append-only
    with sv.session() as s:
        assert s.get(Workspace, sv.ws).current_summary_version == 3
    bad = update(sv, action="rollback", rollback_to=3)
    assert bad.status == "failed" and "different version" in bad.error


def test_summary_workflow_stage_order_and_permissions(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    ex = update(sv)
    stages = [s.stage for s in get_execution(sv, ex.id).steps]
    assert stages == ["LOAD_CURRENT", "GATHER_EVIDENCE", "PROPOSE_SUMMARY", "VALIDATE_PROPOSAL", "COMPARE_WITH_CURRENT", "PERSIST_VERSION",
                      "QUEUE_CONSOLIDATION", "DONE"]
    assert ex.result["followups"], "a consolidation execution should be queued after a new version"


# ---- memory consolidation workflow --------------------------------------------------------------------------------
def test_consolidation_workflow_backfills_deferred_memory_and_refreshes_mental_model(sv):
    sv.settings.hindsight_required = False
    sv.gateway = FlakyGateway(sv.double, fail_first=99)
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    with sv.session() as s:
        assert s.query(ExtractedFinding).filter_by(validated=True, memory_state="pending").count() > 0
    assert sv.double.retain_calls == []
    sv.gateway = sv.double  # Hindsight is back
    eid = sv.runner.create("memory", state=MemoryState(workspace_id=sv.ws, reason="backfill"), workspace_id=sv.ws, idempotency_key="bf1")
    drain(sv)
    ex = get_execution(sv, eid)
    assert ex.status == "succeeded" and ex.result["retained"]["sources"] == 1 and ex.result["consolidation"]["mental_model_id"] == "workspace-overview"
    with sv.session() as s:
        assert s.query(ExtractedFinding).filter_by(validated=True, memory_state="pending").count() == 0
        assert s.query(MemoryOperation).filter_by(op="consolidate", status="done").count() == 1
    assert len(sv.double.retain_calls) == 1 and sv.double.consolidations
    # running it again retains nothing new (idempotent) but may refresh the model
    eid2 = sv.runner.create("memory", state=MemoryState(workspace_id=sv.ws), workspace_id=sv.ws, idempotency_key="bf2")
    drain(sv)
    assert get_execution(sv, eid2).result["retained"]["sources"] == 0 and len(sv.double.retain_calls) == 1


# ---- API -------------------------------------------------------------------------------------------------------------------------
def test_summary_api_roundtrip(sv):
    ingest_pdf(sv, sv.ws, corpus.PAPER_A["filename"], corpus.PAPER_A["pages"])
    with TestClient(create_app(sv.settings, sv)) as c:
        assert c.get(f"/api/workspaces/{sv.ws}/summary").json()["version"] == 0
        r = c.post(f"/api/workspaces/{sv.ws}/summary/update", params={"wait": "true"}).json()
        assert r["status"] == "succeeded" and r["result"]["outcome"] == "created"
        cur = c.get(f"/api/workspaces/{sv.ws}/summary").json()
        assert cur["version"] == 1 and "# " in cur["markdown"]
        statuses = {i["status"] for items in cur["content"]["sections"].values() for i in items}
        assert statuses == {"new"}
        assert c.post(f"/api/workspaces/{sv.ws}/summary/update", params={"wait": "true"}).json()["result"]["outcome"] == "no_change"
        ingest_pdf(sv, sv.ws, corpus.PAPER_D["filename"], corpus.PAPER_D["pages"])
        assert c.post(f"/api/workspaces/{sv.ws}/summary/update", params={"wait": "true"}).json()["result"]["created_version"] == 2
        vs = c.get(f"/api/workspaces/{sv.ws}/summary/versions").json()
        assert [v["version"] for v in vs] == [2, 1]
        d = c.get(f"/api/workspaces/{sv.ws}/summary/diff", params={"from": 1, "to": 2}).json()
        assert d["added"] and d["removed"] == [] and d["meaningful"]
        assert c.get(f"/api/workspaces/{sv.ws}/summary/versions/9").status_code == 404
        rb = c.post(f"/api/workspaces/{sv.ws}/summary/rollback", params={"wait": "true"}, json={"version": 1}).json()
        assert rb["result"]["created_version"] == 3
        drain(sv)  # queued consolidation follow-ups
        mc = c.get(f"/api/workspaces/{sv.ws}/memory/consolidated").json()
        assert mc["available"] and "NOT original evidence" in mc["disclaimer"]
