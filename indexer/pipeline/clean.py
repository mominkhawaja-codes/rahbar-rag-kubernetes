"""Stage 2: Clean. Remove artefacts without destroying structure.

Rules, in the order they run (docs/PIPELINE.md, "Cleaning"):

1. Unicode NFKC, curly quotes to straight ones, the replacement character between two
   letters to an apostrophe ("Rector\\ufffds" -> "Rector's"), no-break spaces to spaces.
2. Running headers and footers go: a line that, with its digits masked, appears on at
   least half of the pages (and on 3 or more) is boilerplate ("Undergraduate Academic
   Rules and Regulations 2026 15").
3. Bare page numbers ("18", "iv") and table-of-contents lines with dot leaders go.
4. Words hyphenated across a line break are re-joined ("regis-\\ntration").
5. Runs of spaces and of blank lines are squeezed.

Deliberately NOT stripped: clause numbers at the start of a line ("5.4 Minimum CGPA"),
heading markers ("## ..."), and table pipes. Chunking and citations depend on all three.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import Document, Page

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", " ": " ", "•": "-",
                         "": "-", "­": ""})
_PAGE_NUMBER = re.compile(r"^(?:page\s*)?(?:\d{1,3}|[ivxlc]{1,6})$", re.I)
_TOC_LINE = re.compile(r"\.{5,}\s*\d{1,3}\s*$")
_HYPHEN_BREAK = re.compile(r"([a-z])-\n([a-z])")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    text = re.sub(r"(?<=[A-Za-z])�(?=[A-Za-z])", "'", text)
    return text.replace("�", "")


def _key(line: str) -> str:
    """A line's identity for header/footer detection: digits masked, markers dropped."""
    # Page-number tokens at either end are dropped, so "... Regulations 2026" and
    # "... Regulations 2026 15" (one footer, with and without its page number) count as
    # one line. Digits inside the line are kept: "5.1 Rule" and "5.2 Rule" stay distinct.
    tokens = line.strip().lstrip("#").lower().split()
    while tokens and tokens[-1].isdigit():
        tokens.pop()
    while tokens and tokens[0].isdigit():
        tokens.pop(0)
    return " ".join(tokens)


def boilerplate_lines(pages: list[Page]) -> set[str]:
    """Keys of lines that repeat on at least half of the pages (and on 3 or more)."""
    if len(pages) < 3:
        return set()
    seen: Counter[str] = Counter()
    for page in pages:
        keys = {_key(line) for line in page.text.splitlines() if line.strip()}
        seen.update(k for k in keys if len(k) <= 100 and not k.startswith("|"))
    threshold = max(3, len(pages) // 2)
    return {k for k, n in seen.items() if n >= threshold}


def clean_page_text(text: str, boilerplate: set[str]) -> str:
    out: list[str] = []
    for raw in normalise(text).splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            out.append("")
            continue
        if _key(line) in boilerplate:
            continue
        # page numbers, also when set in a letter-spaced display font ("1 0", "I V")
        if _PAGE_NUMBER.match(re.sub(r"[#\s]", "", line)) or _TOC_LINE.search(line):
            continue
        out.append(line)
    joined = "\n".join(out)
    joined = _HYPHEN_BREAK.sub(r"\1\2", joined)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    pages_with_text = [p for p in doc.pages if p.text.strip()]
    boilerplate = boilerplate_lines([Page(p.source, p.page, normalise(p.text), p.ocr)
                                     for p in pages_with_text])
    cleaned = [Page(source=p.source, page=p.page, text=clean_page_text(p.text, boilerplate),
                    ocr=p.ocr) for p in doc.pages]
    return Document(source=doc.source, doc_version=doc.doc_version,
                    superseded=doc.superseded, pages=[p for p in cleaned if p.text])
