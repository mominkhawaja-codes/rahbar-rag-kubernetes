"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
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

from .config import IndexerSettings, get_settings
from .models import Chunk


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


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def build_chunks(settings: IndexerSettings) -> tuple[list[Chunk], dict[str, int]]:
    """Extract, clean and chunk every document. No network: safe for a dry run."""
    from .chunk import chunk, count_tokens, heading_of
    from .clean import clean
    from .extract import extract

    documents = discover(settings.corpus_dir)
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    all_chunks: list[Chunk] = []
    totals = {"documents": 0, "pages": 0, "ocr_pages": 0, "source_tokens": 0,
              "stored_tokens": 0}
    for path, doc_version, superseded in documents:
        if not path.exists():
            raise SystemExit(f"listed in _index.json but missing: {path}")
        doc = clean(extract(str(path), doc_version, superseded))
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


def run_pipeline(settings: IndexerSettings) -> int:
    from .embed import embed, embedding_text
    from .store import store

    all_chunks, totals = build_chunks(settings)
    log("corpus chunked", **totals, amplification=_amplification(totals))
    vectors = embed([embedding_text(c) for c in all_chunks], settings)
    log("embedded", vectors=len(vectors), dimension=len(vectors[0]) if vectors else 0,
        embed_model=settings.embed_model)
    return store(all_chunks, vectors, settings)


def _amplification(totals: dict[str, int]) -> float:
    """Stored tokens / original tokens: what overlap and repeated table headers cost."""
    return round(totals["stored_tokens"] / max(1, totals["source_tokens"]), 3)


def dry_run(settings: IndexerSettings, show: int) -> int:
    """Chunk the corpus and print statistics and sample chunks. Writes nothing."""
    all_chunks, totals = build_chunks(settings)
    log("dry run", **totals, amplification=_amplification(totals),
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
    for c in all_chunks[:show]:
        print(json.dumps({"id": c.id, "section": c.section, "page": c.page,
                          "superseded": c.superseded, "text": c.text[:300]},
                         ensure_ascii=False))
    return len(all_chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--dry-run", action="store_true",
                        help="extract, clean and chunk only; print statistics, write nothing")
    parser.add_argument("--show", type=int, default=0, help="with --dry-run, print N chunks")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    if args.dry_run:
        dry_run(settings, args.show)
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
            count = run_pipeline(settings)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
