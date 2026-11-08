"""Stage 1: Extract. PDF, HTML, Markdown or plain text to text, page by page.

What this stage hands to `clean` is plain text with two kinds of markup that the
chunker relies on:

* headings are written as Markdown heading lines (``## 5. Academic Assessment``), so
  section boundaries survive whatever the source format was;
* tables are written as Markdown pipe tables (``| Grade | Points |``), one row per
  line, so a row is never split from its header by a word wrap.

PDF specifics, found on the real corpus (docs/PIPELINE.md, "Extraction"):

* Some FAST PDFs carry the neighbouring page's text *outside* the page's own box
  (negative x coordinates). Plain extraction mixes two pages line by line, so every
  page is cropped to its own bounding box first.
* The prospectus and the student handbook are laid out in two or three columns. The
  column gutters are found from the word positions and each column is read top to
  bottom, left column first.
* Heading lines are recognised by font size: noticeably larger than the page's body
  text. OCR'd pages have no font information; the chunker falls back on clause numbers.
* A page with (almost) no text layer is rasterised and OCR'd with Tesseract, and the
  page is marked ``ocr=True``.
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import Document, Page

log = logging.getLogger("rahbar.indexer.extract")

# A page with fewer visible characters than this has no usable text layer.
MIN_TEXT_CHARS = 25
# A line whose font is this much larger than the body text is a heading.
HEADING_SIZE_RATIO = 1.15
# A gap between word columns at least this wide (in points) is a column gutter.
MIN_GUTTER_WIDTH = 6.0
OCR_DPI = 300


class ExtractionError(RuntimeError):
    """A document could not be read. The Job fails loudly instead of indexing less."""


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    The OCR libraries need system packages in the Dockerfile (tesseract-ocr,
    poppler-utils), not only pip packages.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        pages = _extract_pdf(p)
    elif suffix in {".html", ".htm"}:
        pages = [Page(source=p.name, page=1, text=html_to_text(_read_text(p)))]
    elif suffix == ".md":
        pages = [Page(source=p.name, page=1, text=strip_front_matter(_read_text(p)))]
    elif suffix == ".txt":
        pages = [Page(source=p.name, page=1, text=_read_text(p))]
    else:
        raise ExtractionError(f"unsupported document type: {p.name}")
    return Document(source=p.name, doc_version=doc_version, superseded=superseded, pages=pages)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


# ---------------------------------------------------------------------------- Markdown
def strip_front_matter(text: str) -> str:
    """Drop a leading ``---`` front-matter block (the fixtures carry one)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


# ---------------------------------------------------------------------------- HTML
class _HtmlToText(HTMLParser):
    """Keep headings, paragraphs, list items and tables; drop navigation and scripts."""

    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "form", "svg",
            "iframe", "button", "select"}
    BLOCK = {"p", "div", "li", "br", "section", "article", "ul", "ol", "dd", "dt", "tr",
             "table", "blockquote", "pre"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.buf: list[str] = []
        self.heading: int | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.table_rows: list[list[str]] | None = None
        self.breadcrumb_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = " ".join(v or "" for k, v in attrs if k == "class")
        if self.skip_depth or tag in self.SKIP or "breadcrumb" in classes:
            if tag not in {"br", "img", "input", "meta", "link", "hr"}:
                self.skip_depth += 1
            return
        if tag in self.HEADINGS:
            self._flush()
            self.heading = self.HEADINGS[tag]
        elif tag == "table":
            self._flush()
            self.table_rows = []
        elif tag == "tr" and self.table_rows is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            if tag not in {"br", "img", "input", "meta", "link", "hr"}:
                self.skip_depth -= 1
            return
        if tag in self.HEADINGS and self.heading is not None:
            text = _squash("".join(self.buf))
            self.buf = []
            if text:
                self.out.append("")
                self.out.append("#" * self.heading + " " + text)
            self.heading = None
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(_squash("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table_rows is not None:
            if any(self.row):
                self.table_rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_rows is not None:
            self.out.append("")
            self.out.extend(table_to_markdown(self.table_rows))
            self.out.append("")
            self.table_rows = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.table_rows is None:
            self.buf.append(data)

    def _flush(self) -> None:
        if self.heading is not None:
            return
        text = _squash("".join(self.buf))
        self.buf = []
        if text:
            self.out.append(text)

    def text(self) -> str:
        self._flush()
        return "\n".join(self.out).strip() + "\n"


def html_to_text(raw: str) -> str:
    parser = _HtmlToText()
    parser.feed(raw)
    parser.close()
    text = parser.text()
    # The page title banner repeats the first heading; content starts at the first heading.
    first = re.search(r"^#{1,6} ", text, re.M)
    return text[first.start():] if first else text


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ---------------------------------------------------------------------------- tables
def table_to_markdown(rows: list[list[Any]]) -> list[str]:
    """Render rows as a Markdown pipe table: header row, separator, body rows."""
    clean = [[_squash(str(c or "")).replace("|", "/") for c in row] for row in rows]
    clean = [r for r in clean if any(r)]
    if not clean:
        return []
    width = max(len(r) for r in clean)
    clean = [r + [""] * (width - len(r)) for r in clean]
    # drop columns that are empty in every row (merged cells leave these behind)
    keep = [i for i in range(width) if any(r[i] for r in clean)]
    clean = [[r[i] for i in keep] for r in clean]
    lines = ["| " + " | ".join(clean[0]) + " |",
             "| " + " | ".join("---" for _ in clean[0]) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return lines


def is_real_table(rows: list[list[Any]]) -> bool:
    """pdfplumber reports decorative frames and text boxes as tables. A real table has
    at least two rows and two columns, is mostly filled, and has short cells."""
    cells = [[(c or "").strip() for c in row] for row in rows]
    cells = [r for r in cells if any(r)]
    if len(cells) < 2 or max(len(r) for r in cells) < 2:
        return False
    flat = [c for r in cells for c in r]
    filled = [c for c in flat if c]
    if len(filled) < 0.5 * len(flat):
        return False
    return sum(len(c) for c in filled) / len(filled) < 60


# ---------------------------------------------------------------------------- PDF
def _extract_pdf(path: Path) -> list[Page]:
    import pdfplumber

    pages: list[Page] = []
    ocr_pages: list[int] = []
    with pdfplumber.open(str(path)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            visible = page.within_bbox(page.bbox)   # drop off-page text (see module doc)
            if _visible_chars(visible) < MIN_TEXT_CHARS:
                ocr_pages.append(number)
                pages.append(Page(source=path.name, page=number, text="", ocr=True))
                continue
            pages.append(Page(source=path.name, page=number, text=pdf_page_text(visible)))
    if ocr_pages:
        texts = ocr_pdf_pages(path, ocr_pages)
        for page_obj in pages:
            if page_obj.page in texts:
                page_obj.text = texts[page_obj.page]
        log.info("ocr", extra={"source": path.name, "pages": len(ocr_pages)})
    return pages


def _visible_chars(page: Any) -> int:
    return sum(1 for c in page.chars if c.get("text", "").strip())


def pdf_page_text(page: Any) -> str:
    """One page's text: tables as pipe tables, columns in reading order, headings marked."""
    tables = []
    for table in page.find_tables():
        rows = table.extract()
        if is_real_table(rows):
            tables.append((table.bbox, rows))

    def outside_tables(obj: dict[str, Any]) -> bool:
        x = (obj["x0"] + obj["x1"]) / 2
        y = (obj["top"] + obj["bottom"]) / 2
        return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b, _ in tables)

    body = page.filter(lambda o: o.get("object_type") != "char" or outside_tables(o))
    body_size = _body_font_size(body)

    # Each block is (column index, top, lines): tables become blocks of their own.
    blocks: list[tuple[int, float, list[str]]] = []
    columns = find_columns([w for w in body.extract_words()], page.bbox[0], page.bbox[2])
    for col_index, (x0, x1) in enumerate(columns):
        region = body.filter(
            lambda o, a=x0, b=x1: o.get("object_type") != "char"
            or a <= (o["x0"] + o["x1"]) / 2 < b)
        for line in region.extract_text_lines(return_chars=True):
            text = line["text"].strip()
            if not text:
                continue
            if body_size and _line_size(line) >= body_size * HEADING_SIZE_RATIO \
                    and len(text) <= 90 and not text.isdigit():
                text = "## " + text
            blocks.append((col_index, line["top"], [text]))
    for bbox, rows in tables:
        col_index = next((i for i, (x0, x1) in enumerate(columns) if x0 <= bbox[0] < x1), 0)
        blocks.append((col_index, bbox[1], ["", *table_to_markdown(rows), ""]))
    blocks.sort(key=lambda b: (b[0], b[1]))
    return "\n".join(line for _, _, lines in blocks for line in lines).strip()


def find_columns(words: list[dict[str, Any]], left: float, right: float) -> list[tuple[float, float]]:
    """Split the page into text columns at vertical gutters that no word crosses.

    A full-width line (a running header, a title) would hide every gutter, so a gutter
    may be crossed by a few words: at most 3% of them.
    """
    if len(words) < 40:
        return [(left, right)]
    lo = int(min(w["x0"] for w in words))
    hi = int(max(w["x1"] for w in words)) + 1
    coverage = [0] * (hi - lo + 1)
    for w in words:
        for x in range(int(w["x0"]) - lo, int(w["x1"]) - lo + 1):
            coverage[x] += 1
    allowed = max(1, int(len(words) * 0.03))
    gutters: list[tuple[int, int]] = []
    start = None
    for x, n in enumerate(coverage):
        if n <= allowed and start is None:
            start = x
        elif n > allowed and start is not None:
            if x - start >= MIN_GUTTER_WIDTH:
                gutters.append((start + lo, x + lo))
            start = None
    # Cut where the gutter is emptiest, not at its middle: hanging clause numbers
    # ("4. Semester Course Load") sit inside the low-density band.
    cuts = []
    for g0, g1 in gutters:
        band = coverage[g0 - lo:g1 - lo]
        least = min(band)
        runs, run_start = [], None
        for i, n in enumerate([*band, least + 1]):
            if n == least and run_start is None:
                run_start = i
            elif n != least and run_start is not None:
                runs.append((run_start, i))
                run_start = None
        a, b = max(runs, key=lambda r: r[1] - r[0])
        cut = g0 + (a + b) / 2
        # a gutter must separate two columns that both hold a real share of the words
        left_share = sum(1 for w in words if w["x1"] <= cut) / len(words)
        if 0.15 <= left_share <= 0.85:
            cuts.append(cut)
    # A "column" with a handful of words is hanging clause numbers or a margin note:
    # merge it into the column to its right (or, for the last one, to its left).
    while cuts:
        edges = [left, *cuts, right]
        shares = [sum(1 for w in words if edges[i] <= (w["x0"] + w["x1"]) / 2 < edges[i + 1])
                  / len(words) for i in range(len(edges) - 1)]
        small = next((i for i, s in enumerate(shares) if s < 0.08), None)
        if small is None:
            break
        cuts.pop(small if small < len(cuts) else small - 1)
    edges = [left, *cuts, right]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _body_font_size(page: Any) -> float:
    sizes = Counter(round(c["size"], 1) for c in page.chars if c.get("text", "").strip())
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _line_size(line: dict[str, Any]) -> float:
    sizes = Counter(round(c["size"], 1) for c in line.get("chars", []) if c["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 0.0


# ---------------------------------------------------------------------------- OCR
def ocr_pdf_pages(path: Path, numbers: list[int]) -> dict[int, str]:
    """Rasterise and OCR the given 1-based pages. Needs tesseract and poppler.

    With OCR_ENABLED=false the pages are left empty and the loss is logged: use that
    only for a local dry run, never in the Job, or the scanned document disappears
    from the index without an error.
    """
    if os.environ.get("OCR_ENABLED", "true").lower() == "false":
        log.warning("ocr disabled; scanned pages left empty",
                    extra={"source": path.name, "pages": numbers})
        return {n: "" for n in numbers}
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:  # pragma: no cover - the image installs both
        raise ExtractionError(f"OCR libraries missing: {exc}") from exc

    lang = os.environ.get("OCR_LANG", "eng")
    texts: dict[int, str] = {}
    for n in numbers:
        try:
            images = convert_from_path(str(path), dpi=OCR_DPI, first_page=n, last_page=n)
            texts[n] = "\n".join(pytesseract.image_to_string(img, lang=lang) for img in images)
        except Exception as exc:  # poppler or tesseract missing, or a corrupt page
            raise ExtractionError(f"OCR failed on {path.name} page {n}: {exc}") from exc
    return texts
