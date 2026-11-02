from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from evaldata import corpus
from tests.helpers import drain, make_pdf


@pytest.fixture
def client(services):
    with TestClient(create_app(services.settings, services)) as c:
        c.svc = services
        yield c


def _mk_ws(client, name="Agentic AI"):
    r = client.post("/api/workspaces", json={"name": name, "description": "d"})
    assert r.status_code == 201, r.text
    return r.json()


def test_health_is_honest_about_modes(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["generation_mode"] == "extractive" and h["llm_provider"] == "none"
    assert h["hindsight"]["ok"] is True and h["hindsight"]["version"] == "double"  # the double identifies itself


def test_full_flow_over_http(client):
    ws = _mk_ws(client)
    assert ws["bank_id"].startswith("pra-test-agentic-ai") and ws["sources"] == 0

    r = client.post(f"/api/workspaces/{ws['id']}/sources/pdf", files={"file": ("a.pdf", make_pdf(corpus.PAPER_A["pages"]), "application/pdf")})
    assert r.status_code == 202 and r.json()["state"] == "queued"
    sid, eid = r.json()["source_id"], r.json()["execution_id"]
    dup = client.post(f"/api/workspaces/{ws['id']}/sources/pdf", files={"file": ("again.pdf", make_pdf(corpus.PAPER_A["pages"]), "application/pdf")})
    assert dup.json()["duplicate"] is True and dup.json()["source_id"] == sid

    assert client.get(f"/api/workspaces/{ws['id']}/sources/{sid}").json()["status"] == "queued"
    drain(client.svc)
    src = client.get(f"/api/workspaces/{ws['id']}/sources/{sid}").json()
    assert src["status"] == "completed" and src["passages"] >= 3 and src["findings"]["validated"] >= 3
    text = client.get(f"/api/workspaces/{ws['id']}/sources/{sid}/text").json()
    assert "31%" in text["text"]
    ps = client.get(f"/api/workspaces/{ws['id']}/sources/{sid}/passages").json()
    assert {p["page_start"] for p in ps} == {1, 2}

    hits = client.get(f"/api/workspaces/{ws['id']}/search", params={"q": "hallucination LongQA"}).json()
    assert hits and hits[0]["source_id"] == sid and hits[0]["reasons"]

    a = client.post(f"/api/workspaces/{ws['id']}/ask", json={"question": "How much does MemoryBank-Lite reduce hallucination on LongQA?"})
    assert a.status_code == 200
    body = a.json()
    assert body["answer"]["mode"] == "supported" and body["answer"]["generation_mode"] == "extractive"
    cit = body["answer"]["claims"][0]["citations"][0]
    passage = client.get(f"/api/workspaces/{ws['id']}/passages/{cit['passage_id']}").json()
    assert cit["quote"] in passage["text"]

    # follow-up in the same conversation inherits the topic
    f = client.post(f"/api/workspaces/{ws['id']}/ask", json={"question": "What are its limitations?", "conversation_id": body["conversation_id"]}).json()
    assert f["answer"]["plan"]["intent"] == "source_specific"
    assert any("English" in c["text"] for c in f["answer"]["claims"])
    turns = client.get(f"/api/workspaces/{ws['id']}/conversations/{body['conversation_id']}/turns").json()
    assert len(turns) == 2

    # harness inspector
    ex = client.get(f"/api/executions/{eid}").json()
    assert ex["status"] == "succeeded" and [s["stage"] for s in ex["steps"]][:3] == ["RECEIVED", "VALIDATE_INPUT", "REGISTER_SOURCE"]
    qa_ex = client.get(f"/api/executions/{body['execution_id']}").json()
    retr = next(s for s in qa_ex["steps"] if s["stage"] == "RETRIEVE_SOURCE_EVIDENCE")
    assert retr["detail"]["retrieved"][0]["why"]  # why each passage was considered relevant
    assert client.get("/api/executions", params={"workspace_id": ws["id"], "workflow": "qa"}).json()
    assert {w["name"] for w in client.get("/api/workflows").json()} >= {"ingestion", "qa"}
    ops = client.get(f"/api/workspaces/{ws['id']}/memory/operations").json()
    assert any(o["op"] == "retain" and o["status"] == "done" for o in ops)
    assert client.get(f"/api/workspaces/{ws['id']}").json()["sources"] == 1


def test_invalid_uploads_return_clear_4xx(client):
    ws = _mk_ws(client)
    r = client.post(f"/api/workspaces/{ws['id']}/sources/pdf", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_pdf"
    r = client.post(f"/api/workspaces/{ws['id']}/sources/url", json={"url": "http://localhost:8888/"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "url_blocked_private"
    r = client.post(f"/api/workspaces/{ws['id']}/sources/note", json={"text": "  "})
    assert r.status_code == 400
    assert client.post("/api/workspaces/ws_nope/ask", json={"question": "hi"}).status_code == 404
    assert client.post(f"/api/workspaces/{ws['id']}/ask", json={"question": "  "}).status_code in (400, 500, 502)


def test_oversize_upload_rejected_without_buffering_unbounded(client):
    client.svc.settings.max_upload_mb = 1
    ws = _mk_ws(client)
    r = client.post(f"/api/workspaces/{ws['id']}/sources/pdf", files={"file": ("big.pdf", b"%PDF-1.4" + b"0" * 1_500_000, "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "file_too_large"


def test_note_create_edit_via_api(client):
    ws = _mk_ws(client)
    r = client.post(f"/api/workspaces/{ws['id']}/sources/note", json={"title": "Idea", "text": "I believe hybrid retrieval beats dense-only retrieval for papers."}).json()
    drain(client.svc)
    r2 = client.put(f"/api/workspaces/{ws['id']}/sources/{r['source_id']}/note",
                    json={"title": "Idea", "text": "I now doubt that hybrid retrieval beats dense-only retrieval for papers."})
    assert r2.status_code == 202 and r2.json()["source_id"] == r["source_id"] and not r2.json()["duplicate"]
    drain(client.svc)
    assert client.get(f"/api/workspaces/{ws['id']}/sources/{r['source_id']}").json()["version"] == 2


def test_resume_endpoint(client):
    ws = _mk_ws(client)
    r = client.post(f"/api/workspaces/{ws['id']}/sources/pdf", files={"file": ("c.pdf", b"%PDF-1.4\n" + b"junk " * 500, "application/pdf")}).json()
    drain(client.svc)
    ex = client.get(f"/api/executions/{r['execution_id']}").json()
    assert ex["status"] == "failed" and ex["failed_stage"] == "EXTRACT_CONTENT"
    assert client.post(f"/api/workspaces/{ws['id']}/sources/{r['source_id']}/retry").status_code == 202  # deterministic failure => fails again, visibly
    drain(client.svc)
    ex2 = client.get(f"/api/executions/{r['execution_id']}").json()
    assert ex2["status"] == "failed" and ex2["resume_count"] == 1
    assert client.post(f"/api/executions/{r['execution_id']}/resume").status_code == 202


def test_bearer_token_protects_api(services):
    services.settings.app_api_token = "s3cret-token-value"
    with TestClient(create_app(services.settings, services)) as c:
        assert c.get("/api/health").status_code == 200  # health stays public
        assert c.get("/api/workspaces").status_code == 401
        assert c.get("/api/workspaces", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/workspaces", headers={"Authorization": "Bearer s3cret-token-value"}).status_code == 200
