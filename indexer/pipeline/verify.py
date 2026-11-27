"""Stage 1b: Verify. Runs after extract and clean, before chunking (paper 4.2.2).

Every document gets a pass or fail, with a reason, for four checks:

* quality:      extraction failed, (almost) no text, or garbled text;
* completeness: title, version and page count present; the file is not truncated;
* recency:      a newer version of the same document (same explicit `doc_id`) exists;
* uniqueness:   an exact duplicate (hash) or a near-duplicate (document embedding).

The outcome per document (`action`):

* ``index``      all four pass: the document is chunked and indexed.
* ``quarantine`` quality, completeness or uniqueness failed: the file is moved to the
                 quarantine folder and is not indexed.
* ``supersede``  only recency failed: a newer version exists. The document is *kept* and
                 indexed with ``superseded=True``, so retrieval excludes it through
                 metadata and Part A2 can archive and restore it.

Nothing here invents metadata: a title or version the source does not state is reported
as missing, and the document is quarantined.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import Document, SourceDoc

CHECKS = ("quality", "completeness", "recency", "uniqueness")
EmbedFn = Callable[[list[str]], list[list[float]]]

# Quality thresholds (measured on the real corpus: see docs/PIPELINE.md, "Verification").
MAX_BAD_CHAR_FRACTION = 0.02     # replacement or control characters
MIN_LETTER_RATIO = 0.40          # letters / non-space characters; tables are digit-heavy
# Measured on the real corpus with nomic-embed-text (docs/PIPELINE.md, "Verification"): the
# most similar pair of *different* documents scores 0.9643 (student handbook vs academic rules
# handbook), a lightly edited copy of a document scores 0.993 to 0.998. Hence 0.98 (config.py).
# A document is embedded as the mean of up to this many evenly spaced windows of text.
WINDOWS = 6
WINDOW_CHARS = 1500
_UNKNOWN = {"", "unknown", "none", "null"}


@dataclass
class Check:
    status: str          # "pass" | "fail"
    reason: str

    @property
    def ok(self) -> bool:
        return self.status == "pass"


@dataclass
class Verdict:
    source: str
    path: str
    doc_id: str | None
    title: str | None
    doc_version: str | None
    declared_superseded: bool
    file_sha256: str | None = None
    text_sha256: str | None = None
    pages: int = 0
    characters: int = 0
    # Pages that had no text layer and went to OCR, counted after cleaning, and how many of
    # those still have no text (OCR disabled or a blank page). None: extraction failed.
    ocr_pages: int | None = None
    ocr_pages_empty: int | None = None
    letter_ratio: float | None = None
    checks: dict[str, Check] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)   # reported, never a reason to quarantine
    # "current", or "superseded" when a newer version exists or the source marks it so.
    # A superseded document is indexed (action "index" or "supersede") with superseded=True.
    version_status: str = "current"
    superseded_by: str | None = None                    # the newer file, when one was found
    action: str = "index"
    nearest: dict[str, Any] | None = None      # most similar other document, for the report
    quarantined_to: str | None = None
    quarantine_error: str | None = None


@dataclass
class VerificationRun:
    verdicts: list[Verdict]
    documents: dict[str, Document]        # cleaned documents that will be chunked, by file name
    near_duplicate_check: str             # "done" or why it was skipped


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def document_text(doc: Document) -> str:
    return "\n".join(page.text for page in doc.pages)


def normalise(text: str) -> str:
    """Case and whitespace do not make two documents different."""
    return re.sub(r"\s+", " ", text).strip().lower()


def version_key(version: str | None) -> tuple[int, int, int] | None:
    """Order '2023-08' < '2026-06' < '2026-06-15'. None when the string is not a date-like
    version, so no order is guessed. A year range such as '2019-20' is read as its first year."""
    match = re.match(r"^\s*(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?", version or "")
    if not match:
        return None
    year, month, day = (int(g) if g else 0 for g in match.groups())
    return (year, month if 1 <= month <= 12 else 0, day if 1 <= day <= 31 else 0)


# ------------------------------------------------------------------ the four checks
def check_quality(text: str, error: str | None, min_chars: int) -> tuple[Check, float | None]:
    if error:
        return Check("fail", f"extraction failed: {error}"), None
    stripped = text.strip()
    if len(stripped) < min_chars:
        return Check("fail", f"empty: {len(stripped)} characters of text "
                             f"(minimum {min_chars})"), None
    visible = [c for c in stripped if not c.isspace()]
    bad = sum(1 for c in visible if c == "�" or unicodedata.category(c) == "Cc")
    letters = sum(1 for c in visible if c.isalpha())
    ratio = round(letters / len(visible), 3)
    if bad / len(visible) > MAX_BAD_CHAR_FRACTION:
        return Check("fail", f"garbled: {bad} replacement or control characters "
                             f"({bad / len(visible):.1%})"), ratio
    if ratio < MIN_LETTER_RATIO:
        return Check("fail", f"garbled: only {ratio:.0%} of characters are letters "
                             f"(minimum {MIN_LETTER_RATIO:.0%})"), ratio
    return Check("pass", f"{len(stripped)} characters, {ratio:.0%} letters"), ratio


def _truncation(path: Path, text: str) -> tuple[str | None, str | None]:
    """(strong reason, weak warning) that the file looks cut short. A strong reason is
    structural evidence (a PDF without %%EOF, HTML without a closing html tag) and fails
    completeness. A weak one is only a hint (text ending without punctuation, which many
    valid files do) and is reported as a warning without failing anything."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 2048))
            if b"%%EOF" not in handle.read():
                return "PDF has no %%EOF marker at its end", None
    elif suffix in {".html", ".htm"}:
        if "</html" not in path.read_text(encoding="utf-8", errors="replace").lower():
            return "HTML has no closing </html> tag", None
    elif suffix in {".md", ".txt"}:
        last = text.rstrip()[-1:]
        if last and (last.isalpha() or last == ","):
            return None, f"text may be cut short: ends mid-sentence ({text.rstrip()[-30:]!r})"
    return None, None


def check_completeness(src: SourceDoc, doc: Document | None, text: str,
                       actual_sha: str | None) -> tuple[Check, list[str]]:
    """The check result, plus warnings that are reported but never fail the document."""
    problems: list[str] = []
    warnings: list[str] = []
    if not (src.title or "").strip():
        problems.append("title missing")
    if (src.doc_version or "").strip().lower() in _UNKNOWN:
        problems.append("version/date missing")
    if doc is None:
        problems.append("page count unknown (no extracted text)")
    elif not doc.pages:
        problems.append("page count is 0")
    if actual_sha and src.sha256 and actual_sha != src.sha256:
        problems.append("file hash differs from the recorded sha256 (corrupt or truncated)")
    elif src.size is not None and src.path.exists() and src.path.stat().st_size != src.size:
        problems.append(f"file size {src.path.stat().st_size} differs from the recorded "
                        f"{src.size} bytes (truncated)")
    if doc is not None:
        strong, weak = _truncation(src.path, text)
        if strong:
            problems.append(f"truncated: {strong}")
        if weak:
            warnings.append(weak)
    if problems:
        return Check("fail", "; ".join(problems)), warnings
    pages = len(doc.pages) if doc else 0
    return (Check("pass", f"title, version and {pages} page(s) present; no structural sign "
                          f"of truncation"), warnings)


def document_vector(text: str, embed_fn: EmbedFn) -> list[float]:
    """One unit vector per document: the mean of evenly spaced text windows. Embedding only
    the first page would call two different handbooks with the same cover text duplicates."""
    if len(text) <= WINDOW_CHARS:
        windows = [text]
    else:
        step = (len(text) - WINDOW_CHARS) / (WINDOWS - 1)
        windows = [text[int(i * step): int(i * step) + WINDOW_CHARS] for i in range(WINDOWS)]
    vectors = embed_fn(windows)
    mean = [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]
    norm = sum(x * x for x in mean) ** 0.5 or 1.0
    return [x / norm for x in mean]


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ------------------------------------------------------------------ the stage
def verify_documents(
    sources: list[SourceDoc],
    extract_fn: Callable[[SourceDoc], Document],
    clean_fn: Callable[[Document], Document],
    embed_fn: EmbedFn | None,
    min_chars: int = 200,
    near_dup_threshold: float = 0.98,
) -> VerificationRun:
    """Verify every source. `embed_fn=None` skips the near-duplicate check (a dry run needs
    no model) and says so in each uniqueness reason."""
    verdicts: list[Verdict] = []
    docs: dict[str, Document] = {}
    texts: dict[str, str] = {}

    for src in sources:
        name = src.path.name
        v = Verdict(source=name, path=str(src.path), doc_id=src.doc_id, title=src.title,
                    doc_version=src.doc_version, declared_superseded=src.superseded)
        error = None
        try:
            v.file_sha256 = _hash_file(src.path)
            doc = clean_fn(extract_fn(src))
        except Exception as exc:  # noqa: BLE001 - any failure is a quality failure to report
            doc, error = None, f"{type(exc).__name__}: {exc}"
        text = document_text(doc) if doc else ""
        v.pages, v.characters = (len(doc.pages) if doc else 0), len(text.strip())
        if doc:
            v.ocr_pages = sum(1 for p in doc.pages if p.ocr)
            v.ocr_pages_empty = sum(1 for p in doc.pages if p.ocr and not p.text.strip())
        v.checks["quality"], v.letter_ratio = check_quality(text, error, min_chars)
        v.checks["completeness"], v.warnings = check_completeness(src, doc, text, v.file_sha256)
        if doc and v.checks["quality"].ok:
            docs[name], texts[name] = doc, text
            v.text_sha256 = hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()
        verdicts.append(v)

    near = _check_uniqueness(verdicts, sources, texts, embed_fn, near_dup_threshold)
    _check_recency(verdicts, sources)

    for v in verdicts:
        if not all(v.checks[c].ok for c in ("quality", "completeness", "uniqueness")):
            v.action = "quarantine"
            docs.pop(v.source, None)
        elif not v.checks["recency"].ok:
            v.action = "supersede"
            docs[v.source].superseded = True
        else:
            v.action = "index"
    return VerificationRun(verdicts, docs, near)


def _check_uniqueness(verdicts: list[Verdict], sources: list[SourceDoc],
                      texts: dict[str, str], embed_fn: EmbedFn | None,
                      threshold: float) -> str:
    group = {s.path.name: s.doc_id for s in sources}
    seen: dict[str, str] = {}                       # text hash -> first file with it
    for v in verdicts:
        if v.text_sha256 is None:
            v.checks["uniqueness"] = Check("pass", "no usable text to compare")
            continue
        first = seen.setdefault(v.text_sha256, v.source)
        if first != v.source:
            v.checks["uniqueness"] = Check("fail", f"exact duplicate of {first} "
                                                    f"(same sha256 of the text)")
        else:
            v.checks["uniqueness"] = Check("pass", "no exact duplicate")

    if embed_fn is None:
        for v in verdicts:
            if v.checks["uniqueness"].ok:
                v.checks["uniqueness"].reason += "; near-duplicate check skipped (no embeddings)"
        return "skipped: no embeddings available"

    vectors = {v.source: document_vector(texts[v.source], embed_fn)
               for v in verdicts if v.source in texts}
    retained: set[str] = set()                      # earlier documents that stay unique
    for v in verdicts:
        if v.source not in vectors:
            continue
        best_any: tuple[float, str] | None = None
        best_other: tuple[float, str] | None = None
        for u in verdicts:
            if u.source == v.source or u.source not in vectors:
                continue
            sim = _cosine(vectors[v.source], vectors[u.source])
            if best_any is None or sim > best_any[0]:
                best_any = (sim, u.source)
            # Versions of one document are handled by the recency check, not rejected as
            # near-duplicates: documents that share an explicit doc_id, or two different
            # versions of which the source itself marks one as superseded.
            same_document = (group[v.source] and group[v.source] == group[u.source]) or (
                v.doc_version != u.doc_version and (v.declared_superseded or u.declared_superseded))
            if u.source in retained and not same_document and (best_other is None or sim > best_other[0]):
                best_other = (sim, u.source)
        if best_any:
            v.nearest = {"source": best_any[1], "cosine": round(best_any[0], 4)}
        if v.checks["uniqueness"].ok and best_other and best_other[0] >= threshold:
            v.checks["uniqueness"] = Check(
                "fail", f"near-duplicate of {best_other[1]} (document cosine "
                        f"{best_other[0]:.4f} >= {threshold})")
        if v.checks["uniqueness"].ok:
            retained.add(v.source)
            # Each document is compared with the documents *before* it that stayed in the
            # corpus, so the first one has nothing to be compared with; later ones are
            # compared with it. Say that, and give the nearest document overall.
            overall = (f"nearest document overall: {best_any[1]} at {best_any[0]:.4f}"
                       if best_any else "no other document in this run")
            if best_other:
                note = (f"highest similarity to an earlier retained document: "
                        f"{best_other[1]} at {best_other[0]:.4f}; {overall}")
            else:
                note = (f"no earlier retained document to compare with (later documents are "
                        f"compared with this one); {overall}")
            v.checks["uniqueness"] = Check("pass", f"no exact duplicate; {note} "
                                                   f"(threshold {threshold})")
    return "done"


def _check_recency(verdicts: list[Verdict], sources: list[SourceDoc]) -> None:
    by_name = {v.source: v for v in verdicts}
    groups: dict[str, list[Verdict]] = {}
    for src in sources:
        if src.doc_id:
            groups.setdefault(src.doc_id, []).append(by_name[src.path.name])

    newest: dict[str, Verdict] = {}
    for doc_id, members in groups.items():
        # Only a version that itself passes the other checks can supersede another.
        valid = [m for m in members if version_key(m.doc_version)
                 and all(m.checks[c].ok for c in ("quality", "completeness", "uniqueness"))]
        if len(members) > 1 and valid:
            newest[doc_id] = max(valid, key=lambda m: version_key(m.doc_version))  # type: ignore[arg-type,return-value]

    for v in verdicts:
        key, top = version_key(v.doc_version), newest.get(v.doc_id or "")
        if top and top is not v and key and key < version_key(top.doc_version):  # type: ignore[operator]
            v.version_status, v.superseded_by = "superseded", top.source
            v.checks["recency"] = Check(
                "fail", f"superseded by {top.source} (version {top.doc_version}, "
                        f"this is {v.doc_version}); kept for archival")
        elif v.declared_superseded:
            v.version_status = "superseded"
            v.checks["recency"] = Check(
                "pass", "marked superseded in its source metadata; kept for archival")
        elif v.doc_id and len(groups[v.doc_id]) > 1:
            v.checks["recency"] = Check("pass", f"newest version of {v.doc_id}")
        else:
            v.checks["recency"] = Check("pass", "no other version of this document")


# ------------------------------------------------------------------ side effects
def quarantine(run: VerificationRun, quarantine_dir: str) -> None:
    """Move every failed file into quarantine_dir. A move that fails is recorded, and the
    document stays out of the index either way."""
    target_dir = Path(quarantine_dir)
    for v in run.verdicts:
        if v.action != "quarantine":
            continue
        src = Path(v.path)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            dest = target_dir / src.name
            if dest.exists():
                dest = target_dir / f"{src.stem}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}{src.suffix}"
            shutil.move(str(src), str(dest))
            v.quarantined_to = str(dest)
        except OSError as exc:
            v.quarantine_error = f"{type(exc).__name__}: {exc}"


def write_report(run: VerificationRun, report_dir: str, context: dict[str, Any]) -> Path:
    """Write verification-<UTC timestamp>.json and return its path."""
    out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = out / f"verification-{stamp}.json"
    n = 1
    while path.exists():                         # two runs in the same second
        n += 1
        path = out / f"verification-{stamp}-{n}.json"
    failed = {c: sum(1 for v in run.verdicts if not v.checks[c].ok) for c in CHECKS}
    actions = {a: sum(1 for v in run.verdicts if v.action == a)
               for a in ("index", "supersede", "quarantine")}
    kept = [v for v in run.verdicts if v.action != "quarantine"]
    versions = {k: sum(1 for v in kept if v.version_status == k) for k in ("current", "superseded")}
    body = {
        "run": {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "near_duplicate_check": run.near_duplicate_check, **context},
        "summary": {"documents": len(run.verdicts), "actions": actions, "failed_checks": failed,
                    "indexed_versions": versions,
                    "documents_with_warnings": sum(1 for v in run.verdicts if v.warnings),
                    "ocr_pages": sum(v.ocr_pages or 0 for v in run.verdicts)},
        "documents": [asdict(v) for v in run.verdicts],
    }
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
