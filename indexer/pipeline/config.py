"""Indexer configuration, from the same ConfigMap the API reads (section 4.1)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass


@dataclass(frozen=True)
class IndexerSettings:
    embed_model: str        # EMBED_MODEL: must equal the API's value (section 4.4)
    chroma_host: str
    chroma_port: int
    collection: str
    corpus_dir: str         # where the documents and _index.json sit inside the Job's pod
    chunk_size: int
    chunk_overlap: int
    ollama_url: str
    # Verification stage (section 4.2.2). Quarantine and report directories default to a
    # writable temp location because the Job's root filesystem is read-only; Part F points
    # them at a PersistentVolumeClaim.
    quarantine_dir: str = ""
    report_dir: str = ""
    near_dup_threshold: float = 0.98   # document-level cosine at or above this is a near-duplicate
    min_chars: int = 200               # less extracted text than this counts as empty
    verify_enabled: bool = True
    # Part A2: the persistent document state and the archive of superseded versions. Like
    # the quarantine they default to a writable temp location; Part F mounts a PVC here.
    state_dir: str = ""
    archive_dir: str = ""


def get_settings() -> IndexerSettings:
    scratch = os.path.join(tempfile.gettempdir(), "rahbar")
    return IndexerSettings(
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=int(os.environ.get("CHROMA_PORT", 8000)),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        corpus_dir=os.environ.get("CORPUS_DIR", "/corpus"),
        chunk_size=int(os.environ.get("CHUNK_SIZE", 400)),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", 50)),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        quarantine_dir=os.environ.get("QUARANTINE_DIR", os.path.join(scratch, "quarantine")),
        report_dir=os.environ.get("REPORT_DIR", os.path.join(scratch, "reports")),
        near_dup_threshold=float(os.environ.get("NEAR_DUP_THRESHOLD", 0.98)),
        min_chars=int(os.environ.get("MIN_CHARS", 200)),
        verify_enabled=os.environ.get("VERIFY_ENABLED", "true").lower() != "false",
        state_dir=os.environ.get("STATE_DIR", os.path.join(scratch, "state")),
        archive_dir=os.environ.get("ARCHIVE_DIR", os.path.join(scratch, "archive")),
    )
