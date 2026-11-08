"""Stage 4: Embed. One vector per chunk, from the pinned model only.

The API embeds questions with the same model and the same prefix convention
(api/app/embeddings.py). If you change one, change the other: that is the pin.
"""

from __future__ import annotations

import time

import httpx

from .config import IndexerSettings
from .models import Chunk

BATCH_SIZE = 32
ATTEMPTS = 3
TIMEOUT_SECONDS = 120.0


class EmbeddingError(RuntimeError):
    pass


def document_prefix(model: str) -> str:
    """nomic-embed-text is trained with task prefixes; other models get none."""
    return "search_document: " if "nomic" in model else ""


def embedding_text(c: Chunk) -> str:
    """What gets embedded for a chunk: where it comes from, then what it says. The
    heading carries meaning the clause text often omits ("Academic Warning")."""
    stem = c.source.rsplit(".", 1)[0].replace("-", " ").replace("_", " ")
    return f"{stem} | {c.section}\n{c.text}"


def _post(client: httpx.Client, url: str, payload: dict) -> list[list[float]]:
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.post(url, json=payload)
            response.raise_for_status()
            vectors = response.json().get("embeddings")
            if not isinstance(vectors, list):
                raise EmbeddingError(f"no 'embeddings' in the reply from {url}")
            return vectors
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            if attempt < ATTEMPTS:
                time.sleep(2 ** attempt)
    raise EmbeddingError(f"embedding failed after {ATTEMPTS} attempts: {last}")


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Batch the calls; one request per chunk is slow.
    """
    prefix = document_prefix(settings.embed_model)
    url = f"{settings.ollama_url.rstrip('/')}/api/embed"
    vectors: list[list[float]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [prefix + t for t in texts[start:start + BATCH_SIZE]]
            got = _post(client, url, {"model": settings.embed_model, "input": batch})
            if len(got) != len(batch):
                raise EmbeddingError(f"asked for {len(batch)} vectors, got {len(got)}")
            vectors.extend(got)
    dims = {len(v) for v in vectors}
    if len(dims) > 1:
        raise EmbeddingError(f"mixed vector dimensions from one model: {sorted(dims)}")
    return vectors
