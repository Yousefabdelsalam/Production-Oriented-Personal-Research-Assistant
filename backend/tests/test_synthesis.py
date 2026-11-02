"""Cross-source synthesis: relationships, contradictions, conflict answers, validation rules."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.harness.workflows.synthesis import SynthesisState
from app.main import create_app
from app.models import EvidencePassage, ExtractedFinding, ResearchRelationship, Source
from app.services import relations as rel
from app.services.llm import ModelRuntime, NullEmbedder
from app.services.workspaces import create_workspace
from evaldata import corpus
from tests.conftest import make_settings
from tests.doubles import InMemoryHindsightDouble, ScriptedLLM
from tests.helpers import drain, get_execution, ingest_pdf
from tests.test_vertical_slice import ask
from app.wiring import build_services


@pytest.fixture
def syn(tmp_path):
    """Services with auto-synthesis on (the real pipeline, double Hindsight)."""
    double = InMemoryHindsightDouble()
    svc = build_services(make_settings(tmp_path, auto_synthesize=True), gateway=double, models=ModelRuntime(None, NullEmbedder()))
    svc.sleep = lambda s: None
    svc.double = double
    with svc.session() as s:
        svc.ws = create_workspace(s, svc.settings, "Agentic AI").id
    yield svc
    svc.session_factory.kw["bind"].dispose()


def _load_trio(svc):
    ids = {}
    for p in (corpus.PAPER_A, corpus.PAPER_B, corpus.PAPER_C):
        ids[p["key"]] = ingest_pdf(svc, svc.ws, p["filename"], p["pages"]).source_id
    return ids


def _rels(svc):
    with svc.session() as s:
        out = []
        for r in s.query(ResearchRelationship).filter_by(active=True).all():
            a, b = s.get(ExtractedFinding, r.finding_a_id), s.get(ExtractedFinding, r.finding_b_id)
            out.append((r, a, b))
            s.expunge(r)
        return out


def test_known_contradiction_between_two_papers_is_found_as_hypothesis(syn):
    ids = _load_trio(syn)
    contra = [(r, a, b) for r, a, b in _rels(syn) if r.predicate == "contradicts"]
    assert contra, "the A-vs-C contradiction was not detected"
    pairs = {frozenset({a.source_id, b.source_id}) for _, a, b in contra}
    assert frozenset({ids["A"], ids["C"]}) in pairs
    for r, a, b in contra:
        assert r.status == "hypothesis" and r.detected_by == "heuristic"  # heuristics never claim 'supported'
        assert a.source_id != b.source_id and len(r.evidence) == 2 and r.rationale.startswith("Heuristic")
    # the headline pair: A's 31% claim vs C's 'does not significantly reduce' / 'could not be reproduced'
    texts = [(a.statement, b.statement) for _, a, b in contra]
    assert any("31%" in x + y and ("not significantly" in x + y or "could not be reproduced" in x + y) for x, y in texts)


def test_no_invented_relationships(syn):
    ids = _load_trio(syn)
    cv = ingest_pdf(syn, syn.ws, corpus.PAPER_CV["filename"], corpus.PAPER_CV["pages"]).source_id
    for r, a, b in _rels(syn):
        assert cv not in (a.source_id, b.source_id), "unrelated computer-vision paper got linked to agent papers"
        assert a.source_id != b.source_id
    # A and B both talk about hallucination reduction but do not contradict
    assert not any(r.predicate == "contradicts" and {a.source_id, b.source_id} == {ids["A"], ids["B"]} for r, a, b in _rels(syn))


def test_every_relationship_resolves_to_real_evidence(syn):
    _load_trio(syn)
    with syn.session() as s:
        for r, a, b in _rels(syn):
            for pid in r.evidence:
                assert s.get(EvidencePassage, pid) is not None
            assert {r.evidence[0], r.evidence[1]} == {a.passage_id, b.passage_id}


def test_synthesis_is_idempotent(syn):
    _load_trio(syn)
    n = len(_rels(syn))
    eid = syn.runner.create("synthesis", state=SynthesisState(workspace_id=syn.ws), workspace_id=syn.ws, idempotency_key="again")
    drain(syn)
    assert len(_rels(syn)) == n
    ex = get_execution(syn, eid)
    assert ex.status == "succeeded" and ex.result["created"] == 0 and ex.result["updated"] > 0


def test_conflicting_question_returns_conflicting_mode_with_both_sides(syn):
    ids = _load_trio(syn)
    ans = ask(syn, syn.ws, "Does memory consolidation reduce hallucination on LongQA?").result["answer"]
    assert ans["mode"] == "conflicting", ans["answer"]
    assert ans["conflicts"], "no conflict note"
    cf = ans["conflicts"][0]
    assert cf["status"] == "hypothesis" and "heuristic" in cf["description"].lower()
    assert {s["source_id"] for s in cf["sides"]} == {ids["A"], ids["C"]}
    assert "⚠" in ans["answer"]


def test_single_source_answer_still_surfaces_known_dispute(syn):
    ids = _load_trio(syn)
    ans = ask(syn, syn.ws, "How much does MemoryBank-Lite reduce hallucination on LongQA?").result["answer"]
    assert any("31%" in c["text"] for c in ans["claims"])
    assert ans["mode"] == "conflicting"
    uncited = [s for cf in ans["conflicts"] for s in cf["sides"] if not s["cited"]]
    assert any(s["source_id"] == ids["C"] for s in uncited)  # the dissenting paper is named even though it wasn't cited


def test_user_rejected_relationship_stays_rejected_and_stops_conflicts(syn):
    _load_trio(syn)
    with TestClient(create_app(syn.settings, syn)) as c:
        k = c.get(f"/api/workspaces/{syn.ws}/knowledge").json()
        assert k["concepts"] and any(x["predicate"] == "contradicts" for x in k["relationships"])
        for r in k["relationships"]:
            if r["predicate"] == "contradicts":
                assert c.patch(f"/api/workspaces/{syn.ws}/relationships/{r['id']}", json={"status": "rejected"}).json()["detected_by"] == "user"
    syn.runner.create("synthesis", state=SynthesisState(workspace_id=syn.ws), workspace_id=syn.ws, idempotency_key="rerun")
    drain(syn)
    assert all(r.status == "rejected" for r, a, b in _rels(syn) if r.predicate == "contradicts")  # automation did not overwrite the user
    ans = ask(syn, syn.ws, "Does memory consolidation reduce hallucination on LongQA?").result["answer"]
    assert ans["conflicts"] == [] and ans["mode"] != "conflicting"


def test_relationship_memory_is_retained_with_uncertainty_label(syn):
    _load_trio(syn)
    bank = [b for b in syn.double.banks][0]
    texts = [t for t in syn.double.all_texts(bank) if "Unverified hypothesis" in t]
    assert texts and all("inference, not a statement made by either source" in t for t in texts)


def test_incremental_synthesis_only_considers_pairs_with_the_new_source(syn):
    ids = _load_trio(syn)
    eid = syn.runner.create("synthesis", state=SynthesisState(workspace_id=syn.ws, trigger_source_id=ids["B"]), workspace_id=syn.ws, idempotency_key="inc")
    drain(syn)
    ex = get_execution(syn, eid)
    pairs = next(s for s in ex.steps if s.stage == "SELECT_CANDIDATE_PAIRS").detail["candidate_pairs"]["top"]
    with syn.session() as s:
        for p in pairs:
            assert ids["B"] in {s.get(ExtractedFinding, p["a"]).source_id, s.get(ExtractedFinding, p["b"]).source_id}


# ---- validator + LLM strategy -------------------------------------------------------------------------------
def _find(svc, key_text):
    with svc.session() as s:
        f = next(f for f in s.query(ExtractedFinding).filter_by(validated=True).all() if key_text in f.statement)
        s.expunge(f)
        return f


def test_validator_rejects_invented_unrelated_and_same_source_relationships(syn):
    _load_trio(syn)
    cv = ingest_pdf(syn, syn.ws, corpus.PAPER_CV["filename"], corpus.PAPER_CV["pages"]).source_id
    a1, cvf = _find(syn, "31%"), _find(syn, "mean average precision")
    with syn.session() as s:
        by_id = {f.id: f for f in s.query(ExtractedFinding).filter_by(validated=True).all()}
    ok = rel.validate_proposal(rel.Proposal(a1.id, cvf.id, "supports", "Both papers are clearly related somehow.", "llm", 0.9), by_id)
    assert not ok.ok and any("topical overlap" in r for r in ok.reasons)
    same = [f for f in by_id.values() if f.source_id == a1.source_id and f.id != a1.id][0]
    assert not rel.validate_proposal(rel.Proposal(a1.id, same.id, "extends", "Same paper statements related.", "llm"), by_id).ok
    bad_pred = rel.validate_proposal(rel.Proposal(a1.id, _find(syn, "could not be reproduced").id, "inspired_by", "Made-up predicate for testing.", "llm"), by_id)
    assert not bad_pred.ok
    ghost = rel.validate_proposal(rel.Proposal(a1.id, "fnd_missing", "supports", "Refers to a finding that does not exist.", "llm"), by_id)
    assert not ghost.ok


def test_llm_strategy_can_reach_supported_only_with_corroboration(syn):
    _load_trio(syn)
    a1, c1 = _find(syn, "reduces hallucination by 31%"), _find(syn, "does not significantly reduce hallucination")

    def responder(purpose, user):
        # label pairs by finding statement text found in the prompt
        import re

        blocks = dict(re.findall(r"<<<UNTRUSTED (F\d+[AB]) [^>]*>>>\n(.*?)\n<<<END_UNTRUSTED>>>", user, re.S))
        rels = []
        for lab, text in blocks.items():
            if lab.endswith("A"):
                other = blocks.get(lab[:-1] + "B", "")
                pair = text + " || " + other
                if "31%" in pair and "not significantly" in pair:
                    rels.append({"a": lab, "b": lab[:-1] + "B", "predicate": "contradicts", "rationale": "A reports a 31% reduction; the replication finds no significant reduction.", "confidence": 0.9})
                elif "latency" in pair:
                    rels.append({"a": lab, "b": lab[:-1] + "B", "predicate": "extends", "rationale": "Second paper quantifies latency further than the first.", "confidence": 0.5})
        return {"relationships": rels}

    syn.models = ModelRuntime(ScriptedLLM(responder), NullEmbedder())
    eid = syn.runner.create("synthesis", state=SynthesisState(workspace_id=syn.ws), workspace_id=syn.ws, idempotency_key="llm-run")
    drain(syn)
    ex = get_execution(syn, eid)
    assert ex.status == "succeeded" and ex.model_calls >= 1
    by_pair = {(r.predicate, frozenset({a.id, b.id})): r for r, a, b in _rels(syn)}
    sup = by_pair.get(("contradicts", frozenset({a1.id, c1.id})))
    assert sup is not None and sup.status == "supported" and sup.detected_by == "llm"  # LLM proposal + polarity corroboration
    ext = [r for (p, _), r in by_pair.items() if p == "extends"]
    assert ext and all(r.status == "hypothesis" for r in ext)  # no independent corroboration => stays a hypothesis
