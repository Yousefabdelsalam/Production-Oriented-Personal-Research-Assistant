"""Markdown / plain-text notes -> blocks with section labels from headings."""
from __future__ import annotations

import re

from app.errors import InvalidInput
from app.services.chunking import DocBlock
from app.services.extract_pdf import ExtractedDoc

_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def extract_note(text: str, title: str | None, max_chars: int) -> ExtractedDoc:
    text = (text or "").replace("\r\n", "\n").replace("\x00", "")
    if not text.strip():
        raise InvalidInput("Note is empty.", code="empty_note")
    if len(text) > max_chars:
        raise InvalidInput(f"Note is {len(text)} characters; the maximum is {max_chars}.", code="note_too_large")
    blocks: list[DocBlock] = []
    section: str | None = None
    h1: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            blocks.append(DocBlock(body, page=None, section=section))
        buf.clear()

    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _MD_HEAD.match(line)
        if m:
            flush()
            section = m.group(2).strip()
            if len(m.group(1)) == 1 and h1 is None:
                h1 = section
        else:
            buf.append(line)
    flush()
    if not blocks:  # note consisted of headings only
        blocks.append(DocBlock(text.strip(), None, None))
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "Untitled note")
    return ExtractedDoc(
        title=(title or h1 or first.lstrip("# ").strip() or "Untitled note")[:300],
        blocks=blocks,
        meta={"format": "markdown" if _MD_HEAD.search(text) else "text"},
        quality={"chars": len(text)},
    )
