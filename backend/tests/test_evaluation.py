"""The evaluation workflow itself: runs offline (test double => memory gates skipped, never faked) and live."""
from __future__ import annotations

import pytest

from app.harness.workflows.evaluation import GATES, EvalState
from app.models import EvaluationRun
from tests.helpers import get_execution


def _run(svc):
    ex = svc.runner.start_and_run("evaluation", state=EvalState(label="t"), trigger={"type": "test"}, idempotency_key=f"eval-{id(svc)}")
    with svc.session() as s:
        run = s.get(EvaluationRun, ex.result["run_id"]) if ex.result else None
        if run is not None:
            s.expunge(run)
    return ex, run


def test_offline_evaluation_passes_non_memory_gates_and_marks_memory_gates_skipped(services):
    ex, run = _run(services)
    assert ex.status == "succeeded", ex.error
    stages = [s.stage for s in get_execution(services, ex.id).steps]
    assert stages == ["SETUP_SANDBOX", "INGEST_CORPUS", "EVALUATE_RETRIEVAL_AND_ANSWERS", "EVALUATE_SYNTHESIS", "EVALUATE_MEMORY", "EVALUATE_SUMMARY",
                      "EVALUATE_SAFETY", "EVALUATE_GATES", "RECORD_RESULTS"]
    failed = [g for g in run.gates if g["passed"] is False]
    assert not failed, [(g["name"], g["value"], g["threshold"]) for g in failed]
    skipped = {g["name"] for g in run.gates if g["skipped"]}
    assert skipped == {"memory_recall_usefulness", "memory_in_answers"}  # honest: double != real Hindsight
    assert run.passed is None, "skipped gates must make the overall result inconclusive, not 'passed'"
    assert run.environment["hindsight_live"] is False and run.environment["generation_mode"] == "extractive"
    assert len(run.gates) == len(GATES)
    cases = run.details["cases"]["questions"]
    assert {c["id"] for c in cases} >= {"q_single", "q_cross", "q_none", "q_conflict", "q_note"}
    assert all(c["mode_ok"] for c in cases), [c for c in cases if not c["mode_ok"]]
    m = run.metrics
    assert m["answers"]["citation_validity"] == 1.0 and m["answers"]["groundedness"] >= 0.9
    assert m["synthesis"]["conflict_detection"] == 1.0
    assert all(m["summary"]["checks"].values()), m["summary"]["checks"]
    assert all(m["safety"]["injection_checks"].values()) and all(m["safety"]["isolation_checks"].values())


def test_evaluation_leaves_user_data_untouched(services, ws):
    from app.models import Workspace

    _run(services)
    with services.session() as s:
        assert [w.id for w in s.query(Workspace).all()] == [ws]  # sandbox workspaces live in their own DB


@pytest.mark.hindsight
def test_live_evaluation_all_gates_including_memory(live_services):
    ex, run = _run(live_services)
    assert ex.status == "succeeded", ex.error
    assert run.environment["hindsight_live"] is True
    failed = [(g["name"], g["value"], g["threshold"]) for g in run.gates if g["passed"] is False]
    assert not failed, failed
    assert not [g for g in run.gates if g["skipped"]]
    assert run.passed is True
