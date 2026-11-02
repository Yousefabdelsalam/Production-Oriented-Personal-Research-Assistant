"""Block-aware chunking. A passage's text is ALWAYS an exact substring of the document's normalized full text,
so character offsets, page numbers and verbatim-quote verification stay consistent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.textutil import sentences


@dataclass
class DocBlock:
    text: str
    page: int | None = None
    section: str | None = None


@dataclass
class NormalizedDoc:
    full_text: str
    # (char_start, char_end, page, section) for each block, in order
    spans: list[tuple[int, int, int | None, str | None]] = field(default_factory=list)


@dataclass
class PassageDraft:
    text: str
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    section: str | None


_HYPHEN_WRAP = re.compile(r"(\w)-\n(\w)")


def normalize_blocks(blocks: list[DocBlock]) -> NormalizedDoc:
    """Clean each block and join them into one text with tracked spans."""
    parts: list[str] = []
    spans: list[tuple[int, int, int | None, str | None]] = []
    pos = 0
    for b in blocks:
        t = b.text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        t = _HYPHEN_WRAP.sub(r"\1\2", t)
        # Re-flow: single newlines inside a paragraph become spaces; blank lines separate paragraphs.
        paras = [re.sub(r"[ \t\f\v]*\n[ \t\f\v]*", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
        t = "\n\n".join(re.sub(r"[ \t]{2,}", " ", p) for p in paras if p)
        if not t:
            continue
        if parts:
            pos += 2  # the "\n\n" separator
        spans.append((pos, pos + len(t), b.page, b.section))
        parts.append(t)
        pos += len(t)
    return NormalizedDoc(full_text="\n\n".join(parts), spans=spans)


def chunk_document(doc: NormalizedDoc, max_chars: int = 1000, min_chars: int = 80, overlap_sentences: int = 1) -> list[PassageDraft]:
    """Greedy sentence packing inside each block (so a passage never crosses a page/section boundary)."""
    out: list[PassageDraft] = []
    text = doc.full_text
    for (b_start, b_end, page, section) in doc.spans:
        # sentence offsets within this block
        sent_spans: list[tuple[int, int]] = []
        cursor = b_start
        block_text = text[b_start:b_end]
        for para in re.finditer(r"[^\n]+", block_text):
            p_text, p_off = para.group(0), b_start + para.start()
            local = 0
            for s in sentences(p_text):
                idx = p_text.find(s, local)
                if idx < 0:
                    continue
                sent_spans.append((p_off + idx, p_off + idx + len(s)))
                local = idx + len(s)
        if not sent_spans:
            continue
        i = 0
        while i < len(sent_spans):
            start = sent_spans[i][0]
            j = i
            end = sent_spans[i][1]
            while j + 1 < len(sent_spans) and sent_spans[j + 1][1] - start <= max_chars:
                j += 1
                end = sent_spans[j][1]
            # a single huge sentence: hard split
            while end - start > max_chars * 1.5:
                cut = start + max_chars
                space = text.rfind(" ", start, cut)
                cut = space if space > start else cut
                out.append(PassageDraft(text[start:cut], start, cut, page, page, section))
                start = cut + 1 if text[cut:cut + 1] == " " else cut
            piece = text[start:end]
            if len(piece) >= min_chars or not out or (out and out[-1].page_start != page):
                out.append(PassageDraft(piece, start, end, page, page, section))
            elif out:  # tiny tail: merge into previous passage if contiguous in same block
                prev = out[-1]
                if prev.page_end == page and prev.section == section and prev.char_end <= start:
                    prev.text = text[prev.char_start:end]
                    prev.char_end = end
                else:
                    out.append(PassageDraft(piece, start, end, page, page, section))
            nxt = j + 1 - overlap_sentences if j + 1 < len(sent_spans) and overlap_sentences else j + 1
            i = max(nxt, i + 1)
    return out
