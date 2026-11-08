"""Stage 5: Store. The only code in the whole system that writes to Chroma.

While a re-index runs, the API keeps serving the old collection (ADR 0001):

1. build the whole new index in a staging collection, ``<name>__staging``;
2. check its count equals the number of chunks;
3. rename the live collection to ``<name>__previous``, rename staging to ``<name>``,
   then delete ``__previous``.

The API only sees an incomplete index for the moment between the two renames, never a
half-written one. A failure before step 3 leaves the live collection untouched.

The collection metadata records the pin (section 4.4): ``embed_model`` and
``dimension``. It also records ``index_id``, which the API's semantic cache uses as its
generation: a new index means a new id, so answers cached from the old one are not
served from the new one.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .config import IndexerSettings
from .models import Chunk

ADD_BATCH = 500


def get_client(settings: IndexerSettings) -> Any:
    import chromadb  # the thin client, from chromadb-client in requirements.txt
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port,
                               settings=ChromaSettings(anonymized_telemetry=False))


def _drop(client: Any, name: str) -> None:
    try:
        client.delete_collection(name)
    except Exception:  # did not exist
        pass


def collection_metadata(settings: IndexerSettings, dimension: int, count: int) -> dict:
    return {
        "hnsw:space": "cosine",
        "embed_model": settings.embed_model,
        "dimension": dimension,
        "index_id": uuid.uuid4().hex[:12],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "chunks": count,
    }


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings,
          client: Any = None) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    if not chunks:
        raise ValueError("nothing to store: zero chunks")
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    ids = [c.id for c in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk ids; ids must be unique per document position")

    client = client or get_client(settings)
    live = settings.collection
    staging, previous = f"{live}__staging", f"{live}__previous"

    _drop(client, staging)                    # a crashed earlier run may have left one
    metadata = collection_metadata(settings, len(vectors[0]), len(chunks))
    collection = client.create_collection(name=staging, metadata=metadata)
    for start in range(0, len(chunks), ADD_BATCH):
        part = chunks[start:start + ADD_BATCH]
        collection.add(
            ids=[c.id for c in part],
            documents=[c.text for c in part],
            embeddings=vectors[start:start + ADD_BATCH],
            metadatas=[c.metadata() for c in part],
        )
    count = int(collection.count())
    if count != len(chunks):
        raise RuntimeError(f"staging holds {count} chunks, expected {len(chunks)}")

    # swap: live -> previous, staging -> live, drop previous
    _drop(client, previous)
    try:
        client.get_collection(live).modify(name=previous)
    except Exception:  # first run: there is no live collection yet
        pass
    collection.modify(name=live)
    _drop(client, previous)
    return count
