"""Ingestion: extraction fidelity, invalid input, duplicates, notes, versioning."""
from __future__ import annotations

import pytest
from fpdf import FPDF

from app.errors import ExtractionError, InvalidInput
from app.models import EvidencePassage, ExtractedFinding, MemoryOperation, Source, SourceVersion, WorkflowExecution
from app.services import sources as src_svc
from app.services.chunking import DocBlock, chunk_document, normalize_blocks
from app.services.extract_pdf import extract_pdf, validate_pdf_bytes
from evaldata import corpus
from tests.helpers import drain, get_execution, ingest_note, ingest_pdf, make_pdf


# ---- extraction ------------------------------------------------------------------------------------
def test_pdf_extraction_preserves_pages_and_sections():
    doc = extract_pdf(make_pdf(corpus.PAPER_A["pages"], title="MemoryBank-Lite"), "a.pdf")
    pages = {b.page for b in doc.blocks}
    assert pages == {1, 2}
    assert doc.meta["page_count"] == 2
    secs = {b.section for b in doc.blocks if b.section}
    assert any("Limitations" in s for s in secs) and any("Results" in s for s in secs)
    assert doc.title == "MemoryBank-Lite"


def test_chunk_text_is_exact_substring_with_offsets():
    nd = normalize_blocks([DocBlock("First sentence is here. Second sentence follows it. " * 40, page=3, section="Intro"),
                           DocBlock("Another page entirely. It has its own sentences to chunk.", page=4)])
    drafts = chunk_document(nd, max_chars=300)
    assert len(drafts) > 3
    for d in drafts:
        assert nd.full_text[d.char_start:d.char_end] == d.text
        assert len(d.text) <= 300 * 1.5
    assert {d.page_start for d in drafts} == {3, 4}  # never crosses a page boundary


def test_pdf_with_no_text_layer_is_rejected_clearly():
    pdf = FPDF()
    pdf.add_page()  # blank page => like a scanned image PDF with no text layer
    with pytest.raises(ExtractionError) as ei:
        extract_pdf(bytes(pdf.output()))
    assert ei.value.code == "pdf_no_text" and "OCR" in str(ei.value)


@pytest.mark.parametrize("data,code", [
    (b"", "empty_file"),
    (b"<html>not a pdf</html>", "not_a_pdf"),
    (b"%PDF-1.4\n" + b"0" * 3000, None),  # passes header check, fails parse
])
def test_invalid_pdf_inputs_fail_safely(data, code):
    if code:
        with pytest.raises(InvalidInput) as ei:
            validate_pdf_bytes(data, 1_000_000)
        assert ei.value.code == code
    else:
        validate_pdf_bytes(data, 1_000_000)
        with pytest.raises(ExtractionError):
            extract_pdf(data)


def test_oversized_upload_rejected():
    with pytest.raises(InvalidInput) as ei:
        validate_pdf_bytes(b"%PDF-1.4" + b"x" * 2_000_000, 1_000_000)
    assert ei.value.code == "file_too_large"


def test_submit_rejects_invalid_pdf_before_creating_anything(services, ws):
    with pytest.raises(InvalidInput):
        src_svc.submit_pdf(services, ws, "bad.pdf", b"hello world")
    with services.session() as s:
        assert s.query(Source).count() == 0 and s.query(WorkflowExecution).count() == 0


def test_corrupt_pdf_ends_failed_with_visible_error(services, ws):
    res = src_svc.submit_pdf(services, ws, "corrupt.pdf", b"%PDF-1.4\n" + b"garbage " * 400)
    drain(services)
    with services.session() as s:
        src = s.get(Source, res.source_id)
        assert src.status == "failed" and "EXTRACT_CONTENT" in src.error
        ex = s.get(WorkflowExecution, res.execution_id)
        assert ex.status == "failed" and ex.failed_stage == "EXTRACT_CONTENT"


# ---- duplicates --------------------------------------------------------------------------------------
def test_same_pdf_twice_creates_one_source_and_no_repeated_memory(services, ws, double):
    r1 = ingest_pdf(services, ws, "a.pdf", corpus.PAPER_A["pages"])
    n_retain = len(double.retain_calls)
    with services.session() as s:
        n_ops = s.query(MemoryOperation).filter_by(op="retain").count()
    r2 = src_svc.submit_pdf(services, ws, "renamed-copy.pdf", make_pdf(corpus.PAPER_A["pages"]))
    assert r2.duplicate and r2.state == "duplicate_completed" and r2.source_id == r1.source_id
    assert drain(services) == 0
    with services.session() as s:
        assert s.query(Source).count() == 1
        assert s.query(WorkflowExecution).filter_by(workflow="ingestion").count() == 1
        assert s.query(MemoryOperation).filter_by(op="retain").count() == n_ops
    assert len(double.retain_calls) == n_retain


def test_different_bytes_same_text_is_content_duplicate(services, ws, double):
    """Two different PDF files with identical extracted text => second is marked duplicate, indexed/retained once."""
    r1 = ingest_pdf(services, ws, "a.pdf", corpus.PAPER_A["pages"], title="Title One")
    r2 = ingest_pdf(services, ws, "b.pdf", corpus.PAPER_A["pages"], title="A Different Metadata Title")
    assert r1.source_id != r2.source_id
    with services.session() as s:
        dup = s.get(Source, r2.source_id)
        assert dup.status == "completed" and dup.duplicate_of == r1.source_id and dup.current_version == 0
        assert s.query(EvidencePassage).filter_by(source_id=dup.id).count() == 0
        assert s.query(ExtractedFinding).filter_by(source_id=dup.id).count() == 0
    assert len(double.retain_calls) == 1


def test_duplicate_url_identity_is_canonicalised(services, ws):
    services.fetch_resolver = lambda host, port: ["93.184.216.34"]
    a = src_svc.submit_url(services, ws, "https://Example.com/post#section-2")
    b = src_svc.submit_url(services, ws, "https://example.com:443/post")
    assert not a.duplicate and b.duplicate and a.source_id == b.source_id


# ---- notes -----------------------------------------------------------------------------------------------
def test_note_create_edit_versions_and_unchanged_edit(services, ws, double):
    r1 = ingest_note(services, ws, corpus.NOTE_HYBRID["text"], corpus.NOTE_HYBRID["title"])
    with services.session() as s:
        n = s.get(Source, r1.source_id)
        assert n.status == "completed" and n.current_version == 1 and n.title == corpus.NOTE_HYBRID["title"]
        v1 = s.query(EvidencePassage).filter_by(source_id=n.id, is_current=True).count()
    bank = list(double.banks)[0]
    assert any("hybrid" in t.lower() for t in double.all_texts(bank))

    edited = corpus.NOTE_HYBRID["text"].replace("I believe hybrid search", "I now doubt that pure dense search") + "\nI think BM25 alone is underrated for exact terminology."
    r2 = ingest_note(services, ws, edited, corpus.NOTE_HYBRID["title"], source_id=r1.source_id)
    assert r2.source_id == r1.source_id and not r2.duplicate
    with services.session() as s:
        n = s.get(Source, r1.source_id)
        assert n.current_version == 2 and s.query(Source).count() == 1
        cur = s.query(EvidencePassage).filter_by(source_id=n.id, is_current=True).all()
        old = s.query(EvidencePassage).filter_by(source_id=n.id, is_current=False).all()
        assert cur and old and all(p.source_version_id != old[0].source_version_id for p in cur)
        assert all(f.superseded for f in s.query(ExtractedFinding).filter_by(source_id=n.id, source_version_id=old[0].source_version_id))
    texts = " ".join(double.all_texts(bank))
    assert "BM25 alone" in texts and "I believe hybrid search" not in texts  # memory replaced, not duplicated

    r3 = src_svc.submit_note(services, ws, edited, corpus.NOTE_HYBRID["title"], source_id=r1.source_id)
    assert r3.duplicate and r3.state == "unchanged" and r3.execution_id is None


def test_empty_note_rejected(services, ws):
    with pytest.raises(InvalidInput):
        src_svc.submit_note(services, ws, "   \n ", "x")


def test_logistics_note_yields_no_findings_and_no_memory(services, ws, double):
    r = ingest_note(services, ws, corpus.NOTE_LOGISTICS["text"], corpus.NOTE_LOGISTICS["title"])
    with services.session() as s:
        src = s.get(Source, r.source_id)
        assert src.status == "completed"
        assert s.query(ExtractedFinding).filter_by(source_id=src.id, validated=True).count() == 0
    assert double.retain_calls == []
    ex = get_execution(services, r.execution_id)
    assert "No findings were validated" in " ".join(ex.result["warnings"])


def test_original_content_is_preserved_on_disk(services, ws):
    data = make_pdf(corpus.PAPER_B["pages"])
    res = src_svc.submit_pdf(services, ws, "b.pdf", data)
    drain(services)
    with services.session() as s:
        v = s.query(SourceVersion).filter_by(source_id=res.source_id).one()
        assert (services.settings.data_dir / v.original_path).read_bytes() == data
