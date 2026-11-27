"""Part A2: incremental update and the document lifecycle (paper 4.2.3 and 4.2.4).

The full rebuild in store.py stays as it was (Assignment 02). This module adds the other
way to run the Job: when an earlier run left a state file and the collection it describes
still exists, only what changed is touched.

* **State.** ``index-state.json`` in STATE_DIR records, per document, a stable document id
  (the explicit ``doc_id``, else the file name), the file's sha256, the text hash, the
  document vector, the version, the chunk ids and the status (``active`` or ``archived``).
* **Unchanged** (same file hash): not extracted, not verified again, not embedded: zero
  chunks and zero verification embeddings. Only metadata that changed is updated in place.
* **Added or changed:** only that document is chunked and embedded; its chunks are upserted
  and any of its old chunks that no longer exist are deleted. Other documents are untouched.
* **Deleted** (gone from the lake) or **rejected** by verification: every chunk with that
  source is deleted, and the counts are checked so no orphan is left behind.
* **Archived:** a superseded version keeps its chunks with ``superseded=True``, which
  retrieval already excludes (``where superseded == False``), and a copy of the file goes to
  ARCHIVE_DIR. **Restore** flips the metadata back; nothing is re-embedded.
* **Cache invalidation:** the API's semantic cache keys on the collection's ``index_id``.
  Any run that changes what is indexed gives the collection a new ``index_id``; a run that
  changes nothing keeps it.

The Job stays the only writer to the vector store.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import IndexerSettings
from .models import Chunk, Document, SourceDoc
from .verify import PriorDoc, Verdict

STATE_FILE = "index-state.json"
STATE_VERSION = 1
ACTIVE, ARCHIVED = "active", "archived"
EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class DocState:
    source: str                  # file name; also what every chunk's `source` metadata holds
    doc_id: str                  # the stable document id: explicit doc_id, else the file name
    doc_version: str
    file_sha256: str
    text_sha256: str
    vector: list[float]
    chunk_ids: list[str]
    title: str | None = None
    status: str = ACTIVE
    locked: bool = False         # set by a manual archive/restore; kept until the file changes
    archived_to: str | None = None
    pages: int = 0
    characters: int = 0
    letter_ratio: float | None = None
    ocr_pages: int | None = None
    ocr_pages_empty: int | None = None
    updated_at: str = ""


@dataclass
class IndexState:
    collection: str
    embed_model: str
    chunk_size: int
    chunk_overlap: int
    documents: dict[str, DocState] = field(default_factory=dict)
    version: int = STATE_VERSION


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ state file
def load_state(state_dir: str) -> IndexState | None:
    path = Path(state_dir) / STATE_FILE
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != STATE_VERSION:
        return None
    docs = {name: DocState(**d) for name, d in raw.pop("documents").items()}
    return IndexState(**{**raw, "documents": docs})


def save_state(state: IndexState, state_dir: str) -> Path:
    out = Path(state_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)                      # a crash never leaves half a state file
    return path


def empty_state(settings: IndexerSettings) -> IndexState:
    return IndexState(settings.collection, settings.embed_model, settings.chunk_size,
                      settings.chunk_overlap)


def _compatible(state: IndexState, settings: IndexerSettings) -> str | None:
    """None when the state can be extended, else why a full rebuild is needed."""
    for name in ("collection", "embed_model", "chunk_size", "chunk_overlap"):
        if getattr(state, name) != getattr(settings, name):
            return (f"{name} changed from {getattr(state, name)!r} to "
                    f"{getattr(settings, name)!r}: every chunk must be embedded again")
    return None


def doc_key(src: SourceDoc) -> str:
    return src.doc_id or src.path.name


# ------------------------------------------------------------------ chroma helpers
def _metadata(c: Chunk) -> dict[str, Any]:
    return c.metadata()


def _sources_in(collection: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for meta in (collection.get(include=["metadatas"]).get("metadatas") or []):
        counts[meta["source"]] = counts.get(meta["source"], 0) + 1
    return counts


def count_chunks(collection: Any, source: str) -> int:
    """How many chunks the store holds for one document. Zero proves there is no orphan."""
    return len(collection.get(where={"source": source}, include=["metadatas"])["ids"])


def _new_index_id(collection: Any, total: int) -> str:
    """Give the collection a new index_id so the API's cache generation changes."""
    index_id = uuid.uuid4().hex[:12]
    meta = {**dict(collection.metadata or {}), "index_id": index_id, "built_at": _now(),
            "chunks": total}
    # Chroma refuses to change the distance function after creation, and the collection
    # was created with it, so it is not part of the update.
    meta.pop("hnsw:space", None)
    collection.modify(metadata=meta)
    return index_id


def _archive_copy(src_path: Path, archive_dir: str, key: str, version: str,
                  meta: dict[str, Any] | None = None) -> str | None:
    """Keep a copy of a superseded version, with a `<file>.meta.json` sidecar holding what
    `_index.json` knew about it, so it can be restored when the lake no longer has the file.
    Returns where the copy is, or None if the file is gone."""
    if not src_path.exists():
        return None
    dest = Path(archive_dir) / key / (version or "unknown") / src_path.name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
    if meta is not None:
        dest.with_name(dest.name + ".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(dest)


# ------------------------------------------------------------------ the ingest
def ingest(settings: IndexerSettings, client: Any = None, embed_fn: EmbedFn | None = None,
           force_full: bool = False) -> dict[str, Any]:
    """Verify, then bring the index up to date with the lake. Returns the run's counts."""
    from . import run as runner          # imported here: run.py imports this module lazily
    from .chunk import chunk
    from .embed import embed, embedding_text
    from .store import get_client, store

    started = time.perf_counter()
    sources = runner.discover_sources(settings.corpus_dir, settings.quarantine_dir)
    if not sources:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    for src in sources:
        if not src.path.exists():
            raise SystemExit(f"listed in _index.json but missing: {src.path}")

    client = client or get_client(settings)
    base_embed: EmbedFn = embed_fn or (lambda texts: embed(texts, settings))
    spent = {"verification": 0, "chunks": 0}

    def count_verification(texts: list[str]) -> list[list[float]]:
        spent["verification"] += len(texts)
        return base_embed(texts)

    state = load_state(settings.state_dir)
    collection, mode, reason = None, "full", "no earlier state: building the whole index"
    if force_full:
        reason = "full rebuild requested"
    elif state is not None:
        problem = _compatible(state, settings)
        if problem:
            reason = problem
        else:
            try:
                collection = client.get_collection(settings.collection)
                mode, reason = "incremental", "state and collection found"
            except Exception:  # noqa: BLE001 - the collection is gone: rebuild it
                reason = "the collection named in the state does not exist"
    if mode == "full":
        state = empty_state(settings)
    assert state is not None

    prior = {n: PriorDoc(d.file_sha256, d.text_sha256, d.vector, d.pages, d.characters,
                         d.letter_ratio, d.ocr_pages, d.ocr_pages_empty)
             for n, d in state.documents.items()} if mode == "incremental" else {}
    vrun = runner.run_verification(settings, sources, apply=True, use_embeddings=True,
                                   embed_fn=count_verification, prior=prior)
    verdicts: dict[str, Verdict] = {v.source: v for v in vrun.verdicts}
    by_source = {s.path.name: s for s in sources}

    accepted = [n for n, v in verdicts.items() if v.action != "quarantine"]
    if not accepted:
        raise SystemExit("every document failed verification: nothing to index")
    rejected = [n for n, v in verdicts.items() if v.action == "quarantine"]
    fresh = [n for n in accepted if n in vrun.documents]          # extracted this run
    unchanged = [n for n in accepted if n not in vrun.documents]
    gone = [n for n in state.documents if n not in by_source]     # deleted from the lake
    removed = sorted(set(gone) | {n for n in rejected if n in state.documents})

    def wants_archived(name: str, was: DocState | None) -> bool:
        if was is not None and was.locked and was.file_sha256 == verdicts[name].file_sha256:
            return was.status == ARCHIVED                          # a manual choice stands
        v = verdicts[name]
        return v.action == "supersede" or v.declared_superseded

    # ---- chunk and embed only what is new or changed
    new_chunks: dict[str, list[Chunk]] = {}
    for name in fresh:
        doc: Document = vrun.documents[name]
        doc.superseded = wants_archived(name, state.documents.get(name))
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        for c in pieces:
            c.doc_id, c.content_hash = doc_key(by_source[name]), verdicts[name].file_sha256 or ""
        new_chunks[name] = pieces
    flat = [c for name in fresh for c in new_chunks[name]]
    vectors = base_embed([embedding_text(c) for c in flat]) if flat else []
    spent["chunks"] = len(flat)
    by_chunk = dict(zip((c.id for c in flat), vectors, strict=True))

    summary: dict[str, Any] = {"mode": mode, "reason": reason}
    docs_state: dict[str, DocState] = {}

    def entry(name: str, status_archived: bool, chunk_ids: list[str],
              was: DocState | None) -> DocState:
        v, src = verdicts[name], by_source[name]
        keep_lock = was is not None and was.locked and was.file_sha256 == v.file_sha256
        archived_to = (_archive_copy(
            src.path, settings.archive_dir, doc_key(src), src.doc_version or "unknown",
            {"name": src.title, "file": name, "doc_version": src.doc_version,
             "superseded": src.superseded, "doc_id": src.doc_id, "sha256": v.file_sha256,
             "bytes": src.path.stat().st_size}) if status_archived else None)
        return DocState(
            source=name, doc_id=doc_key(src), doc_version=src.doc_version or "unknown",
            title=src.title,
            file_sha256=v.file_sha256 or "", text_sha256=v.text_sha256 or "",
            vector=[round(x, 6) for x in (vrun.vectors.get(name) or (was.vector if was else []))],
            chunk_ids=chunk_ids, status=ARCHIVED if status_archived else ACTIVE,
            locked=keep_lock, archived_to=archived_to, pages=v.pages, characters=v.characters,
            letter_ratio=v.letter_ratio, ocr_pages=v.ocr_pages,
            ocr_pages_empty=v.ocr_pages_empty, updated_at=_now())

    if mode == "full":
        all_chunks = [c for name in fresh for c in new_chunks[name]]
        total = store(all_chunks, vectors, settings, client)
        for name in fresh:
            docs_state[name] = entry(name, vrun.documents[name].superseded,
                                     [c.id for c in new_chunks[name]], None)
        counts = _full_counts(fresh, new_chunks, total)
        summary.update(documents={"total": len(accepted), "added": sorted(fresh), "changed": [],
                                  "unchanged": [], "deleted": [], "rejected": sorted(rejected),
                                  "metadata_updated": [], "newly_archived": [], "restored": []},
                       chunks=counts, orphans_removed=0)
        collection = client.get_collection(settings.collection)
        summary["index_id"] = dict(collection.metadata or {}).get("index_id")
        summary["index_id_changed"] = True
    else:
        assert collection is not None
        pin = dict(collection.metadata or {})
        if pin.get("embed_model") not in (None, settings.embed_model):
            raise RuntimeError(f"collection was built with {pin.get('embed_model')!r}, "
                               f"not {settings.embed_model!r}: run a full rebuild")
        if vectors and pin.get("dimension") not in (None, len(vectors[0])):
            raise RuntimeError(f"embedding dimension {len(vectors[0])} differs from the "
                               f"collection's {pin.get('dimension')}: run a full rebuild")
        before_id = pin.get("index_id")
        deleted_chunks = upserted = meta_updated = orphans = 0

        # a failed earlier run can leave chunks the state does not know: remove them
        for source in sorted(set(_sources_in(collection)) - set(state.documents)
                             - {n for n in fresh}):
            orphans += count_chunks(collection, source)
            collection.delete(where={"source": source})

        for name in removed:                                   # deleted or rejected documents
            deleted_chunks += count_chunks(collection, name)
            collection.delete(where={"source": name})

        added: list[str] = []
        changed: list[str] = []
        for name in fresh:                                     # new or modified documents
            was = state.documents.get(name)
            pieces = new_chunks[name]
            collection.upsert(ids=[c.id for c in pieces], documents=[c.text for c in pieces],
                              embeddings=[by_chunk[c.id] for c in pieces],
                              metadatas=[_metadata(c) for c in pieces])
            upserted += len(pieces)
            if was is not None:
                stale = sorted(set(was.chunk_ids) - {c.id for c in pieces})
                if stale:
                    collection.delete(ids=stale)
                    deleted_chunks += len(stale)
            (changed if was is not None else added).append(name)
            docs_state[name] = entry(name, vrun.documents[name].superseded,
                                     [c.id for c in pieces], was)

        newly_archived, restored, metadata_updated = [], [], []
        for name in unchanged:                                 # same file: metadata only
            was = state.documents[name]
            archived = wants_archived(name, was)
            version = by_source[name].doc_version or "unknown"
            want = {"superseded": archived, "doc_version": version,
                    "doc_id": doc_key(by_source[name])}
            have = {"superseded": was.status == ARCHIVED, "doc_version": was.doc_version,
                    "doc_id": was.doc_id}
            if want != have:
                collection.update(ids=was.chunk_ids, metadatas=[want] * len(was.chunk_ids))
                meta_updated += len(was.chunk_ids)
                metadata_updated.append(name)
                if archived and was.status != ARCHIVED:
                    newly_archived.append(name)
                if not archived and was.status == ARCHIVED:
                    restored.append(name)
            docs_state[name] = entry(name, archived, was.chunk_ids, was)
            docs_state[name].updated_at = was.updated_at if want == have else _now()

        # prove the store matches the state: per-document counts, no orphans
        per_document = {n: count_chunks(collection, n) for n in docs_state}
        wrong = {n: (c, len(docs_state[n].chunk_ids)) for n, c in per_document.items()
                 if c != len(docs_state[n].chunk_ids)}
        if wrong:
            raise RuntimeError(f"store and state disagree (found, expected): {wrong}")
        stray = set(_sources_in(collection)) - set(docs_state)
        if stray:
            raise RuntimeError(f"orphan chunks for removed documents: {sorted(stray)}")

        total = int(collection.count())
        changed_anything = bool(added or changed or removed or orphans or metadata_updated)
        index_id = _new_index_id(collection, total) if changed_anything else before_id
        summary.update(
            documents={"total": len(accepted), "added": added, "changed": changed,
                       "unchanged": [n for n in unchanged if n not in metadata_updated],
                       "deleted": removed, "rejected": sorted(rejected),
                       "metadata_updated": metadata_updated, "newly_archived": newly_archived,
                       "restored": restored},
            chunks={"embedded": len(flat), "upserted": upserted, "deleted": deleted_chunks,
                    "metadata_updated": meta_updated, "total_after": total},
            orphans_removed=orphans, index_id=index_id,
            index_id_changed=changed_anything)

    new_state = IndexState(settings.collection, settings.embed_model, settings.chunk_size,
                           settings.chunk_overlap, docs_state)
    save_state(new_state, settings.state_dir)
    summary["verification_embeddings"] = spent["verification"]
    summary["chunks"]["by_document"] = {n: len(d.chunk_ids) for n, d in docs_state.items()}
    summary["archived_documents"] = sorted(n for n, d in docs_state.items() if d.status == ARCHIVED)
    summary["seconds"] = round(time.perf_counter() - started, 1)
    return summary


def _full_counts(fresh: list[str], new_chunks: dict[str, list[Chunk]], total: int
                 ) -> dict[str, Any]:
    n = sum(len(new_chunks[name]) for name in fresh)
    return {"embedded": n, "upserted": n, "deleted": 0, "metadata_updated": 0,
            "total_after": total}


# ------------------------------------------------------------------ restore and archive
def set_document_status(settings: IndexerSettings, source: str, status: str,
                        client: Any = None) -> dict[str, Any]:
    """Archive or restore one document by flipping `superseded` on its chunks; nothing is
    embedded. Restoring a version archives the other versions that share its document id, so
    exactly one version of a document is active (a rollback). Both choices are locked: later
    runs keep them until the file itself changes."""
    from .store import get_client

    if status not in (ACTIVE, ARCHIVED):
        raise ValueError(f"unknown status {status!r}")
    state = load_state(settings.state_dir)
    if state is None or source not in state.documents:
        raise SystemExit(f"{source!r} is not in the index state ({settings.state_dir})")
    client = client or get_client(settings)
    collection = client.get_collection(settings.collection)

    target = state.documents[source]
    changes = {source: status}
    if status == ACTIVE:
        for other in state.documents.values():
            if other.source != source and other.doc_id == target.doc_id and other.status == ACTIVE:
                changes[other.source] = ARCHIVED
    updated = 0
    for name, new_status in changes.items():
        doc = state.documents[name]
        if doc.status == new_status and doc.locked:
            continue
        if doc.status != new_status:
            collection.update(ids=doc.chunk_ids,
                              metadatas=[{"superseded": new_status == ARCHIVED}] * len(doc.chunk_ids))
            updated += len(doc.chunk_ids)
        doc.status, doc.locked, doc.updated_at = new_status, True, _now()
        if new_status == ARCHIVED and not doc.archived_to:
            path = Path(settings.corpus_dir) / name
            doc.archived_to = _archive_copy(
                path, settings.archive_dir, doc.doc_id, doc.doc_version,
                {"name": doc.title, "file": name, "doc_version": doc.doc_version,
                 "superseded": False, "doc_id": doc.doc_id if doc.doc_id != name else None,
                 "sha256": doc.file_sha256})
    index_id = _new_index_id(collection, int(collection.count())) if updated else None
    save_state(state, settings.state_dir)
    return {"changes": changes, "chunks_metadata_updated": updated, "index_id": index_id,
            "active": sorted(n for n, d in state.documents.items() if d.status == ACTIVE),
            "archived": sorted(n for n, d in state.documents.items() if d.status == ARCHIVED)}


def find_archived_copy(archive_dir: str, name: str) -> Path | None:
    """The newest archived copy of a file, wherever its version folder is."""
    found = sorted(Path(archive_dir).glob(f"*/*/{name}"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def restore_from_archive(settings: IndexerSettings, name: str, client: Any = None,
                         embed_fn: EmbedFn | None = None) -> dict[str, Any]:
    """Restore a document, from the persistent archive if the index no longer has it.

    * Still indexed (archived, chunks kept): flip it back to active, as ``--restore`` does.
    * Gone from the lake and the index (its file was deleted): copy the archived file back into
      the lake, list it in ``_index.json`` when the lake has one (from its sidecar), ingest it
      again (verified and embedded like any new document) and make that version the active one.
    """
    state = load_state(settings.state_dir)
    if state is not None and name in state.documents:
        return {"from_archive": False, **set_document_status(settings, name, ACTIVE, client)}
    copy = find_archived_copy(settings.archive_dir, name)
    if copy is None:
        raise SystemExit(f"{name!r} is neither indexed nor in the archive ({settings.archive_dir})")
    lake = Path(settings.corpus_dir)
    dest = lake / name
    if not dest.exists():
        lake.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copy, dest)
    index = lake / "_index.json"
    sidecar = copy.with_name(copy.name + ".meta.json")
    if index.exists() and sidecar.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        if not any(e.get("file") == name for e in entries):
            entries.append({k: v for k, v in json.loads(sidecar.read_text(encoding="utf-8")).items()
                            if v is not None})
            index.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = ingest(settings, client=client, embed_fn=embed_fn)
    if name not in load_state(settings.state_dir).documents:        # type: ignore[union-attr]
        raise SystemExit(f"{name!r} was restored to the lake but verification rejected it; "
                         f"see the verification report in {settings.report_dir}")
    result = set_document_status(settings, name, ACTIVE, client)
    return {"from_archive": True, "copied_from": str(copy), "ingest": summary["chunks"], **result}
