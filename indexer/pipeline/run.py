"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run --verify-only [--no-quarantine]   # verification stage + report only
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import IndexerSettings, get_settings
from .models import Chunk, Document, SourceDoc

if TYPE_CHECKING:
    from .verify import EmbedFn, PriorDoc, VerificationRun


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def _markdown_title(path: Path) -> str | None:
    """The document's own first '# ' heading, after any front matter."""
    from .extract import strip_front_matter

    for line in strip_front_matter(path.read_text(encoding="utf-8")).splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def discover_sources(corpus_dir: str, quarantine_dir: str | None = None) -> list[SourceDoc]:
    """List every document to index with what its source says about it.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    A field the source does not state stays None; nothing is invented here. Files that an
    earlier run moved to quarantine are skipped.
    """
    root = Path(corpus_dir)
    quarantined = Path(quarantine_dir) if quarantine_dir else None
    index = root / "_index.json"
    if index.exists():
        found = []
        for e in json.loads(index.read_text(encoding="utf-8")):
            path = root / e["file"]
            if not path.exists() and quarantined and (quarantined / e["file"]).exists():
                continue
            found.append(SourceDoc(
                path=path, doc_version=e.get("doc_version"),
                superseded=bool(e.get("superseded", False)), title=e.get("name") or e.get("title"),
                doc_id=e.get("doc_id"), sha256=e.get("sha256"), size=e.get("bytes")))
        return found
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        md = path.suffix.lower() == ".md"
        meta = _front_matter(path) if md else {}
        found.append(SourceDoc(
            path=path, doc_version=meta.get("doc_version"),
            superseded=meta.get("superseded", "false").lower() == "true",
            title=meta.get("title") or (_markdown_title(path) if md else None),
            doc_id=meta.get("doc_id")))
    return found


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index."""
    return [(s.path, s.doc_version or "unknown", s.superseded)
            for s in discover_sources(corpus_dir)]


def run_verification(settings: IndexerSettings, sources: list[SourceDoc], apply: bool,
                     use_embeddings: bool, embed_fn: "EmbedFn | None" = None,
                     prior: "dict[str, PriorDoc] | None" = None) -> "VerificationRun":
    """Verify the documents; with `apply`, move failures to quarantine and write the report."""
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .verify import quarantine, verify_documents, write_report

    run = verify_documents(
        sources,
        extract_fn=lambda s: extract(str(s.path), s.doc_version or "unknown", s.superseded),
        clean_fn=clean,
        embed_fn=(embed_fn or (lambda texts: embed(texts, settings))) if use_embeddings else None,
        min_chars=settings.min_chars, near_dup_threshold=settings.near_dup_threshold,
        prior=prior)
    if apply:
        quarantine(run, settings.quarantine_dir)
        report = write_report(run, settings.report_dir, {
            "corpus_dir": settings.corpus_dir, "quarantine_dir": settings.quarantine_dir,
            "embed_model": settings.embed_model, "min_chars": settings.min_chars,
            "near_dup_threshold": settings.near_dup_threshold})
        log("verification report written", path=str(report))
    for v in run.verdicts:
        failed = {c: ch.reason for c, ch in v.checks.items() if not ch.ok}
        log("verified", source=v.source, action=v.action, failed=failed or None,
            quarantined_to=v.quarantined_to, quarantine_error=v.quarantine_error)
    return run


def build_chunks(settings: IndexerSettings, apply: bool = True,
                 use_embeddings: bool = True) -> tuple[list[Chunk], dict[str, int]]:
    """Extract, clean, verify and chunk every document.

    Verification (verify.py) sits between clean and chunk: documents that fail are not
    chunked. `apply` moves them to quarantine and writes the report; a dry run passes
    apply=False and use_embeddings=False, so it writes nothing and needs no model.
    """
    from .chunk import chunk, count_tokens, heading_of
    from .clean import clean
    from .extract import extract

    sources = discover_sources(settings.corpus_dir, settings.quarantine_dir)
    if not sources:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    for src in sources:
        if not src.path.exists():
            raise SystemExit(f"listed in _index.json but missing: {src.path}")
    if settings.verify_enabled:
        run = run_verification(settings, sources, apply, use_embeddings)
        documents: list[tuple[Path, Document]] = [
            (s.path, run.documents[s.path.name]) for s in sources if s.path.name in run.documents]
        if not documents:
            raise SystemExit("every document failed verification: nothing to index")
    else:
        documents = [(s.path, clean(extract(str(s.path), s.doc_version or "unknown",
                                            s.superseded))) for s in sources]
    all_chunks: list[Chunk] = []
    totals = {"documents": 0, "pages": 0, "ocr_pages": 0, "source_tokens": 0,
              "stored_tokens": 0}
    for path, doc in documents:
        doc_version, superseded = doc.doc_version, doc.superseded
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        # headings are stored as each chunk's `section`, not in its text: leave them out
        source_tokens = sum(count_tokens(line) for p in doc.pages
                            for line in p.text.splitlines() if heading_of(line.strip()) is None)
        stored_tokens = sum(count_tokens(c.text) for c in pieces)
        ocr_pages = sum(1 for p in doc.pages if p.ocr)
        log("chunked", source=path.name, doc_version=doc_version, superseded=superseded,
            pages=len(doc.pages), ocr_pages=ocr_pages, chunks=len(pieces),
            source_tokens=source_tokens, stored_tokens=stored_tokens)
        totals["documents"] += 1
        totals["pages"] += len(doc.pages)
        totals["ocr_pages"] += ocr_pages
        totals["source_tokens"] += source_tokens
        totals["stored_tokens"] += stored_tokens
        all_chunks.extend(pieces)
    totals["chunks"] = len(all_chunks)
    return all_chunks, totals


def run_pipeline(settings: IndexerSettings, force_full: bool = False) -> int:
    """Index the corpus. With verification on (the default) this is the incremental ingest
    of incremental.py; VERIFY_ENABLED=false keeps the Assignment 02 full rebuild unchanged."""
    if settings.verify_enabled:
        from .incremental import ingest

        summary = ingest(settings, force_full=force_full)
        report = _write_ingest_report(settings, summary)
        log("ingest finished", mode=summary["mode"], reason=summary["reason"],
            documents={k: (len(v) if isinstance(v, list) else v)
                       for k, v in summary["documents"].items()},
            chunks={k: v for k, v in summary["chunks"].items() if k != "by_document"},
            verification_embeddings=summary["verification_embeddings"],
            orphans_removed=summary["orphans_removed"], index_id=summary["index_id"],
            index_id_changed=summary["index_id_changed"], report=str(report))
        return int(summary["chunks"]["total_after"])

    from .embed import embed, embedding_text
    from .store import store

    all_chunks, totals = build_chunks(settings)
    log("corpus chunked", **totals, amplification=_amplification(totals))
    vectors = embed([embedding_text(c) for c in all_chunks], settings)
    log("embedded", vectors=len(vectors), dimension=len(vectors[0]) if vectors else 0,
        embed_model=settings.embed_model)
    return store(all_chunks, vectors, settings)


def _write_ingest_report(settings: IndexerSettings, summary: dict) -> Path:
    out = Path(settings.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path, n = out / f"ingest-{stamp}.json", 1
    while path.exists():
        n += 1
        path = out / f"ingest-{stamp}-{n}.json"
    body = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "collection": settings.collection, "embed_model": settings.embed_model, **summary}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _amplification(totals: dict[str, int]) -> float:
    """Stored tokens / original tokens: what overlap and repeated table headers cost."""
    return round(totals["stored_tokens"] / max(1, totals["source_tokens"]), 3)


def dry_run(settings: IndexerSettings, show: int) -> int:
    """Chunk the corpus and print statistics and sample chunks. Writes nothing."""
    all_chunks, totals = build_chunks(settings, apply=False, use_embeddings=False)
    log("dry run", **totals, amplification=_amplification(totals),
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
    for c in all_chunks[:show]:
        print(json.dumps({"id": c.id, "section": c.section, "page": c.page,
                          "superseded": c.superseded, "text": c.text[:300]},
                         ensure_ascii=False))
    return len(all_chunks)


def verify_only(settings: IndexerSettings, move: bool) -> int:
    """Run the verification stage alone: extract, clean, verify, write the report. Needs the
    embedding model (near-duplicates) but no Chroma. With move=False nothing is moved."""
    sources = discover_sources(settings.corpus_dir, settings.quarantine_dir)
    if not sources:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    run = run_verification(settings, sources, apply=move, use_embeddings=True)
    if not move:
        from .verify import write_report
        path = write_report(run, settings.report_dir, {
            "corpus_dir": settings.corpus_dir, "quarantine_dir": settings.quarantine_dir,
            "embed_model": settings.embed_model, "min_chars": settings.min_chars,
            "near_dup_threshold": settings.near_dup_threshold, "files_moved": False})
        log("verification report written", path=str(path))
    counts = {a: sum(1 for v in run.verdicts if v.action == a)
              for a in ("index", "supersede", "quarantine")}
    log("verification finished", **counts)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--dry-run", action="store_true",
                        help="extract, clean and chunk only; print statistics, write nothing")
    parser.add_argument("--verify-only", action="store_true",
                        help="run only the verification stage and write its report")
    parser.add_argument("--no-quarantine", action="store_true",
                        help="with --verify-only, report failures but do not move any file")
    parser.add_argument("--full-rebuild", action="store_true",
                        help="ignore the saved state and rebuild the whole index")
    parser.add_argument("--restore", metavar="FILE",
                        help="make this archived version active (archives its sibling versions)")
    parser.add_argument("--archive", metavar="FILE",
                        help="archive this document: exclude it from retrieval, keep its chunks")
    parser.add_argument("--show", type=int, default=0, help="with --dry-run, print N chunks")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if args.dry_run:
        dry_run(settings, args.show)
        return 0
    if args.verify_only:
        return verify_only(settings, move=not args.no_quarantine)
    if args.restore or args.archive:
        from .incremental import ARCHIVED, restore_from_archive, set_document_status

        if args.restore:
            result = restore_from_archive(settings, args.restore)
        else:
            result = set_document_status(settings, args.archive, ARCHIVED)
        log("document status changed", **result)
        return 0
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        else:
            count = run_pipeline(settings, force_full=args.full_rebuild)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
