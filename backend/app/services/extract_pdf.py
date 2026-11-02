"""PDF text extraction preserving page references. Fails safely on invalid / empty / scanned / oversized input."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.errors import ExtractionError, InvalidInput
from app.services.chunking import DocBlock

MAX_PAGES = 600
HEADING_RE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+[A-Z][^\n]{2,80}|[A-Z][A-Z \-]{3,60}|(?:Abstract|Introduction|Conclusions?|References|Related Work|Methods?|Results|Discussion|Limitations)\b[^\n]{0,40})$")


@dataclass
class ExtractedDoc:
    title: str
    blocks: list[DocBlock]
    meta: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)


def validate_pdf_bytes(data: bytes, max_bytes: int) -> None:
    if not data:
        raise InvalidInput("Uploaded file is empty.", code="empty_file")
    if len(data) > max_bytes:
        raise InvalidInput(f"File is {len(data) / 1e6:.1f} MB; the maximum is {max_bytes / 1e6:.0f} MB.", code="file_too_large")
    if not data.lstrip()[:5] == b"%PDF-":
        raise InvalidInput("File is not a PDF (missing %PDF- header).", code="not_a_pdf")


def _guess_title(lines: list[str]) -> str | None:
    """Join the leading short lines of page 1 (titles wrap), stopping at author-like lines (commas, digits, @) or sentences."""
    parts: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            if parts:
                break
            continue
        if len(s) >= 100 or re.search(r"[,@\d†‡⋆*]", s) and parts or s.endswith("."):
            break
        parts.append(s)
        if len(parts) == 3:
            break
    title = " ".join(parts)
    return title if 8 < len(title) < 250 else None


def _is_heading(line: str) -> bool:
    line = line.strip()
    return 3 < len(line) <= 90 and not line.endswith((",", ";")) and bool(HEADING_RE.match(line))


def extract_pdf(data: bytes, filename: str = "document.pdf") -> ExtractedDoc:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise ExtractionError("PDF is encrypted and could not be opened.", code="pdf_encrypted") from e
            if not ok:
                raise ExtractionError("PDF is password protected; passwords are never bypassed.", code="pdf_encrypted")
        n_pages = len(reader.pages)
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError, TypeError, AttributeError, IndexError) as e:
        raise ExtractionError(f"PDF could not be parsed: {type(e).__name__}: {str(e)[:160]}", code="pdf_corrupt") from e
    if n_pages == 0:
        raise ExtractionError("PDF has no pages.", code="pdf_empty")
    if n_pages > MAX_PAGES:
        raise InvalidInput(f"PDF has {n_pages} pages; the maximum is {MAX_PAGES}.", code="too_many_pages")

    blocks: list[DocBlock] = []
    empty_pages: list[int] = []
    page_errors: list[int] = []
    section: str | None = None
    first_line: str | None = None
    total = 0
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one bad page must not kill the document, but is reported
            page_errors.append(i)
            text = ""
        text = text.replace("\x00", "")
        if len(text.strip()) < 20:
            empty_pages.append(i)
            continue
        total += len(text)
        lines = [ln for ln in text.split("\n")]
        if first_line is None:
            first_line = _guess_title(lines)
        # Split page into sub-blocks at detected headings so passages carry a section label.
        buf: list[str] = []
        for ln in lines:
            if _is_heading(ln):
                if buf:
                    blocks.append(DocBlock("\n".join(buf), page=i, section=section))
                    buf = []
                section = ln.strip()
            else:
                buf.append(ln)
        if buf:
            blocks.append(DocBlock("\n".join(buf), page=i, section=section))

    letters = sum(c.isalpha() for b in blocks for c in b.text)
    all_chars = sum(len(b.text) for b in blocks) or 1
    replacement = sum(b.text.count("�") for b in blocks)
    quality = {
        "pages": n_pages,
        "empty_pages": empty_pages,
        "page_errors": page_errors,
        "chars": total,
        "alpha_ratio": round(letters / all_chars, 3),
    }
    if total < 100 or len(empty_pages) / n_pages > 0.7:
        raise ExtractionError(
            f"Almost no extractable text ({total} characters across {n_pages} pages). "
            "The PDF is probably scanned images; OCR is not supported.",
            code="pdf_no_text",
        )
    if letters / all_chars < 0.45 or replacement / all_chars > 0.05:
        raise ExtractionError(
            "Extracted text looks garbled (low alphabetic ratio / replacement characters).", code="pdf_garbled"
        )

    info = reader.metadata or {}
    meta_title = (getattr(info, "title", None) or "").strip()
    title = meta_title if len(meta_title) > 3 else (first_line or filename.rsplit(".", 1)[0])
    meta: dict[str, Any] = {"page_count": n_pages, "filename": filename}
    if getattr(info, "author", None):
        meta["author"] = str(info.author)[:200]
    created = getattr(info, "creation_date", None)
    if created:
        meta["published_at"] = created.date().isoformat()
    return ExtractedDoc(title=title[:300], blocks=blocks, meta=meta, quality=quality)
