"""Question embeddings, shared by the Chroma retriever and the semantic cache.

The question is embedded once per request: both callers go through the same small
LRU memo, so the cache lookup and the retrieval do not pay for two model calls.

The indexer embeds documents with the same model and the matching prefix
(indexer/pipeline/embed.py). That pairing is the embedding pin.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from typing import Protocol

import httpx

from .config import Settings
from .errors import DependencyUnavailable


class EmbeddingUnavailable(DependencyUnavailable):
    """The embedding model could not be reached. The API answers 503, not 500."""

    dependency = "embedding_model"


class Embedder(Protocol):
    model: str

    def embed(self, text: str) -> list[float]:
        ...


def query_prefix(model: str) -> str:
    return "search_query: " if "nomic" in model else ""


class OllamaEmbedder:
    def __init__(self, settings: Settings, memo_size: int = 256) -> None:
        self.model = settings.embed_model
        self.url = f"{settings.ollama_url.rstrip('/')}/api/embed"
        self.timeout = settings.embed_timeout
        self._memo: OrderedDict[str, list[float]] = OrderedDict()
        self._memo_size = memo_size

    def embed(self, text: str) -> list[float]:
        if text in self._memo:
            self._memo.move_to_end(text)
            return self._memo[text]
        payload = {"model": self.model, "input": [query_prefix(self.model) + text]}
        last: Exception | None = None
        for attempt in range(2):                     # one retry on a transient error
            try:
                response = httpx.post(self.url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                vector = response.json()["embeddings"][0]
                break
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = exc
                if attempt == 0:
                    time.sleep(0.5)
        else:
            raise EmbeddingUnavailable(f"embedding model {self.model} unavailable: {last}")
        self._memo[text] = vector
        if len(self._memo) > self._memo_size:
            self._memo.popitem(last=False)
        return vector


class HashEmbedder:
    """No model: a deterministic vector from the normalised text. With it the semantic
    cache only matches the same question (modulo case and spacing). Used with the stub
    retriever and in tests."""

    model = "hash-64"

    def embed(self, text: str) -> list[float]:
        seed = " ".join(text.lower().split()).encode()
        values: list[float] = []
        while len(values) < 64:
            seed = hashlib.sha256(seed).digest()
            values.extend((b - 127.5) / 127.5 for b in seed)
        norm = math.sqrt(sum(v * v for v in values[:64])) or 1.0
        return [v / norm for v in values[:64]]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def get_embedder(settings: Settings) -> Embedder:
    if settings.retriever == "chroma":
        return OllamaEmbedder(settings)
    return HashEmbedder()
