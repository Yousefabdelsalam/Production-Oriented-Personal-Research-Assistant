"""Deterministic PDF generation for tests and the evaluation suite (same pages => same bytes => same sha256)."""
from __future__ import annotations

from datetime import datetime, timezone

from fpdf import FPDF


def make_pdf(pages: list[str], title: str | None = None) -> bytes:
    pdf = FPDF()
    pdf.set_creation_date(datetime(2025, 1, 1, tzinfo=timezone.utc))
    if title:
        pdf.set_title(title)
    pdf.set_auto_page_break(auto=True, margin=15)
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())
