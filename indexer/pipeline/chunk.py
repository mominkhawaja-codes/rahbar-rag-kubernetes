"""Stage 3: Chunk. Structure-aware, with overlap.

Unit of size: a *token* here is a whitespace-separated word (``len(text.split())``).
It is model-independent and cheap; nomic-embed-text's own tokenizer produces roughly
1.3 of its tokens per word on this corpus' English, well inside its context window.

How a document becomes chunks (docs/PIPELINE.md, "Chunking"):

1. Lines are grouped into *sections* at headings: Markdown heading lines (``## ...``,
   written by extract for large-font PDF lines and HTML ``<h2>``) and numbered chapter
   titles (``5. Academic Assessment``). A chunk never crosses a section boundary.
2. Inside a section, lines are grouped into *units*: one numbered clause
   (``5.4 Minimum CGPA ...``), one paragraph, or one whole table.
3. Units are packed greedily into chunks of at most ``size`` tokens. The next chunk in
   the same section starts with the last ``overlap`` tokens of the previous one, so a
   sentence that straddles a boundary is whole in at least one chunk.
4. A unit larger than ``size`` is split on sentences (then words); a table is split on
   rows and every piece repeats the header row, so no row loses its column names.

Each chunk's ``section`` is the heading it sits under plus the clause numbers it holds,
for example ``Academic Warning/Probation [5.4-5.6]``. Citations and recall@k match on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Chunk, Document

_MD_HEADING = re.compile(r"^#{1,6}\s+(.+)$")
_CHAPTER = re.compile(r"^(\d{1,2})\.\s+([A-Z][^.:;|]{2,80})$")
# A clause starts with its number and then a capital letter ("5.4 Minimum CGPA ..."), which
# keeps a sentence that happens to start with a number ("3.99 would be ...") out.
_CLAUSE = re.compile(r"^(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\.?\s+[A-Z(\"']")
_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+")


@dataclass
class _Unit:
    page: int
    kind: str = "text"                 # "text" or "table"
    clause: str | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    @property
    def tokens(self) -> int:
        return len(self.text.split())


@dataclass
class _Section:
    heading: str
    units: list[_Unit] = field(default_factory=list)


def count_tokens(text: str) -> int:
    return len(text.split())


def heading_of(line: str) -> str | None:
    """The heading text if this line is a heading, else None."""
    m = _MD_HEADING.match(line)
    if m:
        return m.group(1).strip().strip("#").strip()
    m = _CHAPTER.match(line)
    if m and len(line.split()) <= 10:
        return line.strip()
    return None


def split_sections(doc: Document) -> list[_Section]:
    sections = [_Section(heading="")]
    unit: _Unit | None = None

    def flush() -> None:
        nonlocal unit
        if unit is not None and unit.text:
            sections[-1].units.append(unit)
        unit = None

    for page in doc.pages:
        for raw in page.text.splitlines():
            line = raw.strip()
            if not line:
                if unit is not None and unit.kind == "text" and unit.clause is None:
                    flush()            # paragraph break; a clause may span blank lines
                continue
            heading = heading_of(line)
            if heading is not None:
                flush()
                sections.append(_Section(heading=heading))
                continue
            if line.startswith("|"):
                if unit is None or unit.kind != "table":
                    flush()
                    unit = _Unit(page=page.page, kind="table")
                unit.lines.append(line)
                continue
            clause = _CLAUSE.match(line)
            if clause or (unit is not None and unit.kind == "table"):
                flush()
                unit = _Unit(page=page.page, clause=clause.group(1) if clause else None)
            elif unit is None:
                unit = _Unit(page=page.page)
            unit.lines.append(line)
    flush()
    return [s for s in sections if s.units]


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    """Split one long unit into pieces of at most `size` tokens, sentence-aligned where
    possible, each piece starting with `overlap` tokens of the previous one."""
    sentences = [s for s in _SENTENCE_END.split(text) if s.strip()]
    words_of = [s.split() for s in sentences]
    pieces: list[list[str]] = []
    current: list[str] = []
    fresh = 0                                    # words in `current` beyond the overlap

    def cut() -> None:
        nonlocal current, fresh
        pieces.append(current)
        current = current[-overlap:] if overlap else []
        fresh = 0

    for words in words_of:
        if fresh and len(current) + len(words) > size:
            cut()
        while len(current) + len(words) > size:  # a sentence longer than a whole chunk
            room = size - len(current)
            current = current + words[:room]
            words = words[room:]
            fresh += room
            cut()
        current = current + words
        fresh += len(words)
    if fresh:                                    # not just the overlap tail of the last piece
        pieces.append(current)
    return [" ".join(p) for p in pieces]


def _split_table(unit: _Unit, size: int) -> list[str]:
    header, body = unit.lines[:2], unit.lines[2:]
    if not body or count_tokens(unit.text) <= size:
        return [unit.text]
    pieces, current = [], list(header)
    for row in body:
        if len(current) > 2 and count_tokens("\n".join(current + [row])) > size:
            pieces.append("\n".join(current))
            current = list(header)
        current.append(row)
    pieces.append("\n".join(current))
    return pieces


def _label(heading: str, clauses: list[str], page: int) -> str:
    heading = heading[:80].strip()
    if clauses:
        rng = clauses[0] if clauses[0] == clauses[-1] else f"{clauses[0]}-{clauses[-1]}"
        return f"{heading} [{rng}]" if heading else f"Clause {rng}"
    return heading or f"Page {page}"


def _slug(source: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError(f"need size > 0 and 0 <= overlap < size, got {size}, {overlap}")

    # (text, page, clauses) pieces in order, per section
    out: list[Chunk] = []

    def emit(heading: str, text: str, page: int, clauses: list[str]) -> None:
        out.append(Chunk(
            id=f"{_slug(doc.source)}-{len(out):04d}", text=text.strip(), source=doc.source,
            section=_label(heading, clauses, page), page=page,
            doc_version=doc.doc_version, superseded=doc.superseded))

    for section in split_sections(doc):
        buffer: list[str] = []          # texts of units in the current chunk
        clauses: list[str] = []
        page = section.units[0].page
        carry = ""                      # overlap tail from the previous chunk

        def flush_buffer() -> None:
            nonlocal buffer, clauses, carry
            if not buffer:
                return
            text = "\n".join(buffer)
            emit(section.heading, text, page, clauses)
            words = text.split()
            carry = " ".join(words[-overlap:]) if overlap else ""
            buffer, clauses = [], []

        for unit in section.units:
            if unit.tokens > size:
                flush_buffer()
                pieces = (_split_table(unit, size) if unit.kind == "table"
                          else _split_text(unit.text, size, overlap))
                for piece in pieces:
                    emit(section.heading, piece, unit.page, [unit.clause] if unit.clause else [])
                carry = " ".join(pieces[-1].split()[-overlap:]) if overlap else ""
                continue
            current = count_tokens("\n".join(buffer))
            if buffer and current + unit.tokens > size:
                flush_buffer()
            if not buffer:
                page = unit.page
                # start with the overlap tail, unless it would push the unit over size
                if carry and count_tokens(carry) + unit.tokens <= size:
                    buffer.append(carry)
            buffer.append(unit.text)
            if unit.clause:
                clauses.append(unit.clause)
        flush_buffer()
    return out
