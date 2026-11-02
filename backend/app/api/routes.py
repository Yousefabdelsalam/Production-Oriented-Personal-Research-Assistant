from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import DB, get_services, require_token
from app.api.serializers import (
    execution_dict, finding_dict, memory_op_dict, passage_dict, relationship_dict, source_dict,
)
from app.container import Services
from app.errors import InvalidInput, NotFound
from app.models import (
    ChatTurn, Conversation, EvidencePassage, ExtractedFinding, MemoryOperation, Source, SourceVersion, WorkflowExecution, Workspace,
)
from app.services import sources as src_svc
from app.services.retrieval import search_evidence
from app.services.workspaces import create_workspace, get_workspace, workspace_stats

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
public = APIRouter(prefix="/api")


def _ws(db: Session, ws_id: str) -> Workspace:
    return get_workspace(db, ws_id)


def _source(db: Session, ws_id: str, sid: str) -> Source:
    s = db.get(Source, sid)
    if s is None or s.workspace_id != ws_id:  # workspace boundary: ids from other workspaces look like 404
        raise NotFound("source not found in this workspace")
    return s


# ---- health ----------------------------------------------------------------------------------------------------------
@public.get("/health")
def health(services: Services = Depends(get_services)) -> dict[str, Any]:
    hs = services.gateway.health()
    db_ok = True
    try:
        with services.session() as s:
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    cap = services.models.describe()
    return {
        "status": "ok" if db_ok else "degraded", "database": {"ok": db_ok},
        "hindsight": {"ok": hs.ok, "version": hs.version, "detail": hs.detail, "base_url": services.settings.hindsight_base_url},
        **cap,
        "worker": {"enabled": services.settings.worker_enabled},
        "notes": ([] if hs.ok else ["Hindsight is unreachable: ingestion will stop at RETAIN_USEFUL_MEMORY (retryable) and answers run without memory."])
                + (["No LLM configured: running deterministic extractive mode (no model calls)."] if cap["generation_mode"] == "extractive" else []),
    }


# ---- workspaces --------------------------------------------------------------------------------------------------------
class WorkspaceIn(BaseModel):
    name: str
    description: str = ""


@router.get("/workspaces")
def list_workspaces(db: Session = DB):
    return [workspace_stats(db, w) for w in db.query(Workspace).order_by(Workspace.created_at).all()]


@router.post("/workspaces", status_code=201)
def post_workspace(body: WorkspaceIn, db: Session = DB, services: Services = Depends(get_services)):
    ws = create_workspace(db, services.settings, body.name, body.description)
    db.flush()
    return workspace_stats(db, ws)


@router.get("/workspaces/{ws_id}")
def get_ws(ws_id: str, db: Session = DB):
    return workspace_stats(db, _ws(db, ws_id))


# ---- sources --------------------------------------------------------------------------------------------------------------
class UrlIn(BaseModel):
    url: str
    refresh: bool = False


class NoteIn(BaseModel):
    text: str
    title: str | None = None


def _submit_payload(res) -> dict[str, Any]:
    return {"source_id": res.source_id, "execution_id": res.execution_id, "duplicate": res.duplicate, "state": res.state, "message": res.message}


@router.get("/workspaces/{ws_id}/sources")
def list_sources(ws_id: str, q: str | None = None, status: str | None = None, kind: str | None = None, db: Session = DB):
    _ws(db, ws_id)
    qs = db.query(Source).filter(Source.workspace_id == ws_id)
    if status:
        qs = qs.filter(Source.status == status)
    if kind:
        qs = qs.filter(Source.kind == kind)
    if q:
        qs = qs.filter(Source.title.ilike(f"%{q}%"))
    return [source_dict(s) for s in qs.order_by(Source.created_at.desc()).all()]


@router.post("/workspaces/{ws_id}/sources/pdf", status_code=202)
async def upload_pdf(ws_id: str, file: UploadFile = File(...), services: Services = Depends(get_services)):
    limit = services.settings.max_upload_mb * 1_000_000
    data = await file.read(limit + 1)  # bounded read: never buffer more than the limit + 1 byte
    return _submit_payload(src_svc.submit_pdf(services, ws_id, file.filename or "document.pdf", data))


@router.post("/workspaces/{ws_id}/sources/url", status_code=202)
def add_url(ws_id: str, body: UrlIn, services: Services = Depends(get_services)):
    return _submit_payload(src_svc.submit_url(services, ws_id, body.url, body.refresh))


@router.post("/workspaces/{ws_id}/sources/note", status_code=202)
def add_note(ws_id: str, body: NoteIn, services: Services = Depends(get_services)):
    return _submit_payload(src_svc.submit_note(services, ws_id, body.text, body.title))


@router.put("/workspaces/{ws_id}/sources/{sid}/note", status_code=202)
def edit_note(ws_id: str, sid: str, body: NoteIn, services: Services = Depends(get_services)):
    return _submit_payload(src_svc.submit_note(services, ws_id, body.text, body.title, source_id=sid))


@router.get("/workspaces/{ws_id}/sources/{sid}")
def get_source(ws_id: str, sid: str, db: Session = DB):
    return source_dict(_source(db, ws_id, sid), detail=True, db=db)


@router.get("/workspaces/{ws_id}/sources/{sid}/text")
def source_text(ws_id: str, sid: str, version: int | None = None, db: Session = DB):
    s = _source(db, ws_id, sid)
    v = db.query(SourceVersion).filter_by(source_id=s.id, version=version or s.current_version).first()
    if v is None:
        raise NotFound("no extracted text yet for this source/version")
    return {"source_id": s.id, "version": v.version, "text": v.extracted_text, "meta": v.meta, "quality": v.quality,
            "original_available": bool(v.original_path), "original_mime": v.original_mime}


@router.get("/workspaces/{ws_id}/sources/{sid}/passages")
def source_passages(ws_id: str, sid: str, db: Session = DB):
    s = _source(db, ws_id, sid)
    ps = db.query(EvidencePassage).filter_by(source_id=s.id, is_current=True).order_by(EvidencePassage.ordinal).all()
    return [passage_dict(p, s) for p in ps]


@router.get("/workspaces/{ws_id}/sources/{sid}/findings")
def source_findings(ws_id: str, sid: str, db: Session = DB):
    s = _source(db, ws_id, sid)
    return [finding_dict(f) for f in db.query(ExtractedFinding).filter_by(source_id=s.id, superseded=False).order_by(ExtractedFinding.created_at).all()]


@router.post("/workspaces/{ws_id}/sources/{sid}/retry", status_code=202)
def retry_source(ws_id: str, sid: str, db: Session = DB, services: Services = Depends(get_services)):
    _source(db, ws_id, sid)
    db.commit()
    return {"execution_id": src_svc.retry_source(services, sid)}


@router.get("/workspaces/{ws_id}/passages/{pid}")
def get_passage(ws_id: str, pid: str, db: Session = DB):
    p = db.get(EvidencePassage, pid)
    if p is None or p.workspace_id != ws_id:
        raise NotFound("passage not found in this workspace")
    d = passage_dict(p, db.get(Source, p.source_id))
    d["context_before"] = _neighbour(db, p, -1)
    d["context_after"] = _neighbour(db, p, +1)
    return d


def _neighbour(db: Session, p: EvidencePassage, delta: int) -> str | None:
    n = db.query(EvidencePassage).filter_by(source_version_id=p.source_version_id, ordinal=p.ordinal + delta).first()
    return n.text if n else None


@router.get("/workspaces/{ws_id}/search")
def search(ws_id: str, q: str = Query(min_length=1), k: int = 10, source_id: str | None = None,
           db: Session = DB, services: Services = Depends(get_services)):
    _ws(db, ws_id)
    hits = search_evidence(db, ws_id, q, k=min(k, 30), source_ids=[source_id] if source_id else None, embedder=services.models.embedder)
    return [h.model_dump() for h in hits]


# ---- chat --------------------------------------------------------------------------------------------------------------------
class AskIn(BaseModel):
    question: str
    conversation_id: str | None = None


@router.post("/workspaces/{ws_id}/conversations", status_code=201)
def new_conversation(ws_id: str, db: Session = DB):
    _ws(db, ws_id)
    c = Conversation(workspace_id=ws_id, title="")
    db.add(c)
    db.flush()
    return {"id": c.id, "workspace_id": ws_id, "created_at": c.created_at}


@router.get("/workspaces/{ws_id}/conversations")
def list_conversations(ws_id: str, db: Session = DB):
    _ws(db, ws_id)
    out = []
    for c in db.query(Conversation).filter_by(workspace_id=ws_id).order_by(Conversation.created_at.desc()).limit(50).all():
        first = db.query(ChatTurn).filter_by(conversation_id=c.id).order_by(ChatTurn.created_at).first()
        out.append({"id": c.id, "created_at": c.created_at, "title": c.title or (first.question[:80] if first else "New conversation")})
    return out


@router.get("/workspaces/{ws_id}/conversations/{cid}/turns")
def conversation_turns(ws_id: str, cid: str, db: Session = DB):
    c = db.get(Conversation, cid)
    if c is None or c.workspace_id != ws_id:
        raise NotFound("conversation not found in this workspace")
    return [{"id": t.id, "question": t.question, "answer": t.answer, "execution_id": t.execution_id, "created_at": t.created_at}
            for t in db.query(ChatTurn).filter_by(conversation_id=cid).order_by(ChatTurn.created_at).all()]


@router.post("/workspaces/{ws_id}/ask")
def ask(ws_id: str, body: AskIn, db: Session = DB, services: Services = Depends(get_services)):
    from app.harness.workflows.qa import QAState

    _ws(db, ws_id)
    if body.conversation_id:
        c = db.get(Conversation, body.conversation_id)
        if c is None or c.workspace_id != ws_id:
            raise NotFound("conversation not found in this workspace")
    else:
        c = Conversation(workspace_id=ws_id)
        db.add(c)
    db.commit()
    ex = services.runner.start_and_run(
        "qa", state=QAState(workspace_id=ws_id, question=body.question, conversation_id=c.id), workspace_id=ws_id,
        trigger={"type": "question", "conversation_id": c.id},
    )
    if ex.status != "succeeded":
        raise HTTPException(status_code=502 if ex.failed_stage else 500, detail={"code": "qa_failed", "message": ex.error, "execution_id": ex.id})
    return {"execution_id": ex.id, "conversation_id": c.id, "answer": ex.result["answer"], "experience": ex.result.get("experience")}


# ---- knowledge explorer -------------------------------------------------------------------------------------------------------------
def _side(db: Session, f: ExtractedFinding, titles: dict[str, str]) -> dict[str, Any]:
    return {"finding_id": f.id, "statement": f.statement, "quote": f.quote, "kind": f.kind, "epistemic": f.epistemic, "page": f.page,
            "section": f.section, "passage_id": f.passage_id, "source_id": f.source_id, "source_title": titles.get(f.source_id, "")}


@router.get("/workspaces/{ws_id}/knowledge")
def knowledge(ws_id: str, db: Session = DB):
    from collections import defaultdict

    from app.models import ResearchRelationship
    from app.services.textutil import STOPWORDS, stem

    _ws(db, ws_id)
    titles = {s.id: s.title for s in db.query(Source).filter_by(workspace_id=ws_id).all()}
    fs = db.query(ExtractedFinding).filter_by(workspace_id=ws_id, validated=True, superseded=False).all()
    by_id = {f.id: f for f in fs}
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"forms": defaultdict(int), "findings": set(), "sources": set()})
    for f in fs:
        for c in f.concepts or []:
            c = c.strip().lower()
            if len(c) < 3 or c in STOPWORDS or c.replace(".", "").isdigit():
                continue
            key = " ".join(sorted({stem(w) for w in c.split()}))
            g = groups[key]
            g["forms"][c] += 1
            g["findings"].add(f.id)
            g["sources"].add(f.source_id)
    concepts = sorted(
        ({"name": max(g["forms"], key=g["forms"].get), "finding_count": len(g["findings"]), "source_count": len(g["sources"]),
          "sources": [{"id": s, "title": titles.get(s, "")} for s in sorted(g["sources"])], "finding_ids": sorted(g["findings"])[:30]}
         for g in groups.values()), key=lambda c: (-c["source_count"], -c["finding_count"], c["name"]))[:80]
    rels = db.query(ResearchRelationship).filter_by(workspace_id=ws_id, active=True).all()
    out_rels = []
    for r in rels:
        a, b = by_id.get(r.finding_a_id), by_id.get(r.finding_b_id)
        if a is None or b is None:
            continue
        out_rels.append({**relationship_dict(r), "a": _side(db, a, titles), "b": _side(db, b, titles)})
    order = {"supported": 0, "hypothesis": 1, "rejected": 2}
    out_rels.sort(key=lambda r: (order.get(r["status"], 3), r["predicate"]))
    return {"concepts": concepts, "relationships": out_rels,
            "stats": {"findings": len(fs), "sources": len({f.source_id for f in fs}), "relationships": len(out_rels),
                      "note": "Heuristic relationships are shown as hypotheses; only LLM-corroborated or user-confirmed links are 'supported'."}}


class RelStatusIn(BaseModel):
    status: str


@router.patch("/workspaces/{ws_id}/relationships/{rid}")
def set_relationship_status(ws_id: str, rid: str, body: RelStatusIn, db: Session = DB):
    from app.models import ResearchRelationship

    r = db.get(ResearchRelationship, rid)
    if r is None or r.workspace_id != ws_id:
        raise NotFound("relationship not found in this workspace")
    if body.status not in ("supported", "hypothesis", "rejected"):
        raise InvalidInput("status must be supported, hypothesis or rejected", code="bad_status")
    r.status, r.detected_by = body.status, "user"  # a user decision is never overwritten by later automated runs
    return relationship_dict(r)


@router.post("/workspaces/{ws_id}/synthesize", status_code=202)
def synthesize(ws_id: str, db: Session = DB, services: Services = Depends(get_services)):
    from uuid import uuid4

    from app.harness.workflows.synthesis import SynthesisState

    _ws(db, ws_id)
    db.commit()
    eid = services.runner.create("synthesis", state=SynthesisState(workspace_id=ws_id), workspace_id=ws_id,
                                 idempotency_key=f"synth:manual:{ws_id}:{uuid4().hex[:8]}", trigger={"type": "manual"})
    return {"execution_id": eid}


# ---- living summary ---------------------------------------------------------------------------------------------------------------------
def _summary_payload(v, markdown: bool = True) -> dict[str, Any]:
    from app.api.serializers import summary_dict
    from app.services.summary import SummaryItem, item_status

    d = summary_dict(v, markdown=markdown)
    for items in d["content"].get("sections", {}).values():
        for it in items:
            it["status"] = item_status(SummaryItem.model_validate(it), v.version)  # derived, never model-asserted
    return d


def _summary_version(db: Session, ws_id: str, n: int):
    from app.models import SummaryVersion

    v = db.query(SummaryVersion).filter_by(workspace_id=ws_id, version=n).first()
    if v is None:
        raise NotFound(f"summary version {n} not found in this workspace")
    return v


@router.get("/workspaces/{ws_id}/summary")
def current_summary(ws_id: str, db: Session = DB):
    ws = _ws(db, ws_id)
    if ws.current_summary_version <= 0:
        return {"version": 0, "message": "No summary yet. Add sources, then generate one (it is only created if it validates)."}
    return _summary_payload(_summary_version(db, ws_id, ws.current_summary_version))


@router.get("/workspaces/{ws_id}/summary/versions")
def summary_versions(ws_id: str, db: Session = DB):
    from app.models import SummaryVersion

    _ws(db, ws_id)
    rows = db.query(SummaryVersion).filter_by(workspace_id=ws_id).order_by(SummaryVersion.version.desc()).all()
    return [{"version": v.version, "kind": v.kind, "parent_version": v.parent_version, "created_at": v.created_at,
             "summary": (v.change or {}).get("summary"), "sources": len(v.source_ids or []), "execution_id": v.execution_id} for v in rows]


@router.get("/workspaces/{ws_id}/summary/diff")
def summary_diff(ws_id: str, from_version: int = Query(alias="from"), to_version: int = Query(alias="to"), db: Session = DB):
    from app.services.summary import diff_contents, parse_content

    _ws(db, ws_id)
    a, b = _summary_version(db, ws_id, from_version), _summary_version(db, ws_id, to_version)
    return {"from": from_version, "to": to_version, **diff_contents(parse_content(a.content), parse_content(b.content))}


@router.get("/workspaces/{ws_id}/summary/versions/{n}")
def summary_version(ws_id: str, n: int, db: Session = DB):
    _ws(db, ws_id)
    return _summary_payload(_summary_version(db, ws_id, n))


class RollbackIn(BaseModel):
    version: int


def _queue_summary(services: Services, ws_id: str, state, wait: bool, trigger: dict[str, Any]):
    from uuid import uuid4

    eid = services.runner.create("summary", state=state, workspace_id=ws_id, idempotency_key=f"summary:{trigger['type']}:{ws_id}:{uuid4().hex[:8]}", trigger=trigger)
    if wait and services.runner.claim(eid):
        ex = services.runner.run(eid)
        return {"execution_id": eid, "status": ex.status, "result": ex.result, "error": ex.error}
    return {"execution_id": eid, "status": "queued"}


@router.post("/workspaces/{ws_id}/summary/update", status_code=202)
def update_summary(ws_id: str, wait: bool = False, db: Session = DB, services: Services = Depends(get_services)):
    from app.harness.workflows.summary import SummaryState

    _ws(db, ws_id)
    db.commit()
    return _queue_summary(services, ws_id, SummaryState(workspace_id=ws_id), wait, {"type": "manual_update"})


@router.post("/workspaces/{ws_id}/summary/rollback", status_code=202)
def rollback_summary(ws_id: str, body: RollbackIn, wait: bool = False, db: Session = DB, services: Services = Depends(get_services)):
    from app.harness.workflows.summary import SummaryState

    _ws(db, ws_id)
    _summary_version(db, ws_id, body.version)
    db.commit()
    return _queue_summary(services, ws_id, SummaryState(workspace_id=ws_id, action="rollback", rollback_to=body.version), wait,
                          {"type": "rollback", "to": body.version})


@router.get("/workspaces/{ws_id}/memory/consolidated")
def consolidated(ws_id: str, db: Session = DB, services: Services = Depends(get_services)):
    from app.harness.workflows.memory import MODEL_ID
    from app.services.hindsight_gateway import HindsightError

    ws = _ws(db, ws_id)
    try:
        view = services.gateway.get_consolidated(ws.bank_id, MODEL_ID)
    except HindsightError as e:
        raise HTTPException(status_code=503, detail={"code": "hindsight_unavailable", "message": str(e)}) from e
    return {"available": view is not None, "view": view,
            "disclaimer": "Generated by Hindsight from retained memory. It is a consolidated memory view, NOT original evidence; verify claims against cited sources."}


@router.post("/workspaces/{ws_id}/memory/consolidate", status_code=202)
def consolidate_memory(ws_id: str, db: Session = DB, services: Services = Depends(get_services)):
    from uuid import uuid4

    from app.harness.workflows.memory import MemoryState

    _ws(db, ws_id)
    db.commit()
    eid = services.runner.create("memory", state=MemoryState(workspace_id=ws_id, reason="manual"), workspace_id=ws_id,
                                 idempotency_key=f"consolidate:manual:{ws_id}:{uuid4().hex[:8]}", trigger={"type": "manual"})
    return {"execution_id": eid}


# ---- evaluation --------------------------------------------------------------------------------------------------------------------------
def _eval_dict(r, full: bool = False) -> dict[str, Any]:
    d = {"id": r.id, "suite": r.suite, "started_at": r.started_at, "finished_at": r.finished_at, "passed": r.passed, "gates": r.gates,
         "environment": r.environment, "metrics": r.metrics}
    if full:
        d["details"] = r.details
    return d


@router.get("/evaluations")
def list_evaluations(limit: int = 20, db: Session = DB):
    from app.models import EvaluationRun

    return [_eval_dict(r) for r in db.query(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(min(limit, 100)).all()]


@router.get("/evaluations/{rid}")
def get_evaluation(rid: str, db: Session = DB):
    from app.models import EvaluationRun

    r = db.get(EvaluationRun, rid)
    if r is None:
        raise NotFound("evaluation run not found")
    return _eval_dict(r, full=True)


@router.post("/evaluations/run", status_code=202)
def run_evaluation(services: Services = Depends(get_services)):
    from uuid import uuid4

    from app.harness.workflows.evaluation import EvalState

    label = uuid4().hex[:6]
    eid = services.runner.create("evaluation", state=EvalState(label=label), idempotency_key=f"eval:{label}", trigger={"type": "manual"})
    return {"execution_id": eid, "note": "Runs the deterministic corpus in an isolated sandbox; takes a few minutes with live Hindsight."}


# ---- memory inspection -----------------------------------------------------------------------------------------------------------
@router.get("/workspaces/{ws_id}/memory/operations")
def memory_operations(ws_id: str, limit: int = 100, db: Session = DB):
    _ws(db, ws_id)
    rows = db.query(MemoryOperation).filter_by(workspace_id=ws_id).order_by(MemoryOperation.created_at.desc()).limit(min(limit, 500)).all()
    return [memory_op_dict(m) for m in rows]


@router.get("/workspaces/{ws_id}/memory/recall")
def memory_recall(ws_id: str, q: str = Query(min_length=1), db: Session = DB, services: Services = Depends(get_services)):
    ws = _ws(db, ws_id)
    from app.services.hindsight_gateway import HindsightError

    try:
        mems = services.gateway.recall(ws.bank_id, q, max_tokens=1500, budget="low")
    except HindsightError as e:
        raise HTTPException(status_code=503, detail={"code": "hindsight_unavailable", "message": str(e)}) from e
    return [m.__dict__ for m in mems]


# ---- executions (harness inspector) --------------------------------------------------------------------------------------------------
@router.get("/executions")
def list_executions(workspace_id: str | None = None, workflow: str | None = None, status: str | None = None, source_id: str | None = None,
                    limit: int = 50, db: Session = DB):
    q = db.query(WorkflowExecution)
    if workspace_id:
        q = q.filter(WorkflowExecution.workspace_id == workspace_id)
    if workflow:
        q = q.filter(WorkflowExecution.workflow == workflow)
    if status:
        q = q.filter(WorkflowExecution.status == status)
    if source_id:
        q = q.filter(WorkflowExecution.source_id == source_id)
    return [execution_dict(e) for e in q.order_by(WorkflowExecution.created_at.desc()).limit(min(limit, 200)).all()]


@router.get("/executions/{eid}")
def get_execution(eid: str, db: Session = DB):
    e = db.get(WorkflowExecution, eid)
    if e is None:
        raise NotFound("execution not found")
    return execution_dict(e, steps=True)


@router.post("/executions/{eid}/resume", status_code=202)
def resume_execution(eid: str, services: Services = Depends(get_services)):
    if not services.runner.resume(eid):
        raise InvalidInput("only failed or interrupted executions can be resumed", code="not_resumable")
    return {"execution_id": eid, "status": "queued"}


@router.get("/workflows")
def workflow_definitions(services: Services = Depends(get_services)):
    return [d.describe() for d in services.runner.defs.values()]
