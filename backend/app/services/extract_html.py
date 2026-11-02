"""Readable-article extraction from HTML (no JS execution, no remote subresources are fetched)."""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.errors import ExtractionError
from app.services.chunking import DocBlock
from app.services.extract_pdf import ExtractedDoc

_DROP = ["script", "style", "noscript", "template", "svg", "iframe", "form", "nav", "footer", "aside", "header", "button", "dialog"]
_BLOCK_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote", "tr", "figcaption", "dd", "dt"]
_NEG_CLASS = re.compile(r"(comment|sidebar|cookie|banner|promo|advert|share|social|related|newsletter|breadcrumb|menu|modal|popup)", re.I)


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def _link_density(tag: Tag) -> float:
    total = len(tag.get_text(" ", strip=True)) or 1
    links = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return links / total


def _pick_main(soup: BeautifulSoup) -> Tag:
    for sel in ("article", "main", '[role="main"]'):
        cands = [t for t in soup.select(sel) if len(t.get_text(" ", strip=True)) > 300]
        if cands:
            return max(cands, key=lambda t: len(t.get_text(" ", strip=True)))
    best, best_score = None, 0.0
    for div in soup.find_all(["div", "section"]):
        if _NEG_CLASS.search(" ".join(div.get("class", [])) + " " + (div.get("id") or "")):
            continue
        ps = div.find_all("p", recursive=True)
        txt = sum(len(p.get_text(" ", strip=True)) for p in ps)
        score = txt * (1 - _link_density(div))
        if score > best_score:
            best, best_score = div, score
    return best or soup.body or soup


def extract_html(html: str, url: str | None = None, min_chars: int = 200) -> ExtractedDoc:
    soup = BeautifulSoup(html, "html.parser")
    title = _meta(soup, "og:title", "twitter:title", "citation_title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    meta: dict[str, Any] = {}
    pub = _meta(soup, "article:published_time", "citation_publication_date", "citation_date", "datePublished", "date", "dc.date")
    if not pub:
        t = soup.find("time", attrs={"datetime": True})
        pub = t["datetime"] if t else None
    if pub:
        meta["published_at"] = pub[:40]
    for key, names in {"author": ("author", "citation_author", "article:author"), "site_name": ("og:site_name",), "description": ("og:description", "description")}.items():
        v = _meta(soup, *names)
        if v:
            meta[key] = v[:300]
    canon = soup.find("link", rel="canonical")
    if canon and canon.get("href"):
        meta["canonical_url"] = str(canon["href"])[:500]

    for t in soup(_DROP):
        t.decompose()
    for t in list(soup.find_all(True)):
        if t.attrs is None or t.decomposed:  # already removed together with a decomposed ancestor
            continue
        cls = " ".join(t.get("class", []) or []) + " " + (t.get("id") or "")
        if t.name not in ("html", "body", "article", "main") and _NEG_CLASS.search(cls) and len(t.get_text(" ", strip=True)) < 800:
            t.decompose()
    main = _pick_main(soup)
    h1 = main.find("h1") or soup.find("h1")
    if not title and h1:
        title = h1.get_text(" ", strip=True)

    blocks: list[DocBlock] = []
    section: str | None = None
    for el in main.find_all(_BLOCK_TAGS):
        # Containers that wrap other captured blocks are skipped; their children are captured instead.
        if el.name in ("li", "blockquote") and el.find(["p", "li", "blockquote"]):
            continue
        text = el.get_text(" ", strip=True) if el.name != "pre" else el.get_text("\n")
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            section = text[:200]
            continue
        if el.name in ("p", "li", "blockquote", "dd", "figcaption") and len(text) < 25 and not text.endswith("."):
            continue
        blocks.append(DocBlock(f"- {text}" if el.name == "li" else text, None, section))
    total = sum(len(b.text) for b in blocks)
    if total < min_chars:
        raise ExtractionError(
            f"Could not extract readable article content ({total} characters). The page may require JavaScript, "
            "a login, or be mostly navigation.",
            code="no_readable_content",
        )
    return ExtractedDoc(
        title=(title or url or "Untitled page")[:300],
        blocks=blocks,
        meta=meta,
        quality={"chars": total, "blocks": len(blocks)},
    )
