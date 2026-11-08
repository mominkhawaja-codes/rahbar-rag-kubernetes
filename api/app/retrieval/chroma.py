"""The real retriever (sections 3.2 and 4.4).

* The question is embedded with settings.embed_model, the model the indexer used.
* Superseded documents are filtered out in the query itself (``where``), so a repealed
  clause can only reach the model when the question explicitly asks about an older
  version ("previous", "old", "superseded", "2023 rules" ...).
* Chroma returns cosine *distance*; Passage.score is similarity = 1 - distance.
* The embedding pin: the collection's ``embed_model`` metadata must equal EMBED_MODEL,
  and the question vector must have the collection's ``dimension``. A mismatch makes
  /readyz fail and /api/ask answer 503; it is never a silent wrong answer.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from ..config import Settings
from ..embeddings import Embedder, OllamaEmbedder
from ..errors import DependencyUnavailable
from ..schemas import Passage

log = logging.getLogger("rahbar.retrieval")

# Words that mean "I am asking about an older version on purpose".
_HISTORY = re.compile(
    r"\b(previous|previously|old|older|earlier|former|formerly|superseded|repealed|"
    r"used to|before 20\d\d|in 20(1\d|2[0-5]) rules?|20(1\d|2[0-5]) (edition|version|rules))\b",
    re.I)


class RetrievalUnavailable(DependencyUnavailable):
    """Chroma or the collection is unusable. The API answers 503."""

    dependency = "chroma"


class EmbeddingPinMismatch(RetrievalUnavailable):
    """The index was built with a different embedding model than EMBED_MODEL."""

    dependency = "embed_pin"


def wants_history(question: str) -> bool:
    return bool(_HISTORY.search(question))


def pin_problem(metadata: dict[str, Any] | None, embed_model: str) -> str | None:
    """None when the collection was built with `embed_model`, else the reason it was not."""
    recorded = (metadata or {}).get("embed_model")
    if recorded is None:
        return "the collection does not record its embed_model; re-run the indexer"
    if recorded != embed_model:
        return (f"embedding model mismatch: index built with {recorded!r}, "
                f"API configured with EMBED_MODEL={embed_model!r}")
    return None


class ChromaRetriever:
    name = "chroma"
    METADATA_TTL = 30.0   # seconds between re-reads of the collection metadata

    def __init__(self, settings: Settings, embedder: Embedder | None = None,
                 client: Any = None) -> None:
        self.settings = settings
        self.embedder = embedder or OllamaEmbedder(settings)
        self._client = client
        self._collection: Any = None
        self._meta: dict[str, Any] = {}
        self._meta_at = 0.0

    # ------------------------------------------------------------------ connection
    @property
    def client(self) -> Any:
        """Connect on first use, not in __init__: a slow Chroma must not stop the API
        process from starting (it only stops it from being *ready*)."""
        if self._client is None:
            import chromadb  # the thin client, from chromadb-client in requirements.txt
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host, port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False))
        return self._client

    def collection(self, refresh: bool = False) -> Any:
        now = time.monotonic()
        if refresh or self._collection is None or now - self._meta_at > self.METADATA_TTL:
            try:
                self._collection = self.client.get_collection(self.settings.collection)
            except Exception as exc:
                self._collection = None
                raise RetrievalUnavailable(
                    f"collection {self.settings.collection!r} unavailable: {exc}") from exc
            self._meta = dict(self._collection.metadata or {})
            self._meta_at = now
        return self._collection

    def index_id(self) -> str:
        """Identifies the index build; the semantic cache keys on it (invalidation)."""
        try:
            self.collection()
        except RetrievalUnavailable:
            return "unavailable"
        return str(self._meta.get("index_id") or self._meta.get("built_at") or "unknown")

    # ------------------------------------------------------------------ query
    def retrieve(self, question: str, k: int) -> list[Passage]:
        collection = self.collection()
        problem = pin_problem(self._meta, self.settings.embed_model)
        if problem:
            raise EmbeddingPinMismatch(problem)
        vector = self.embedder.embed(question)
        dimension = self._meta.get("dimension")
        if dimension is not None and int(dimension) != len(vector):
            raise EmbeddingPinMismatch(
                f"embedding dimension mismatch: index has {dimension}, "
                f"{self.settings.embed_model} returned {len(vector)}")

        where = None if wants_history(question) else {"superseded": False}
        try:
            result = collection.query(query_embeddings=[vector], n_results=k, where=where,
                                      include=["documents", "metadatas", "distances"])
        except Exception as exc:
            self._collection = None          # reconnect next time
            raise RetrievalUnavailable(f"chroma query failed: {exc}") from exc

        passages = []
        ids = result.get("ids", [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        for chunk_id, text, meta, distance in zip(ids, docs, metas, dists):
            meta = meta or {}
            page = meta.get("page")
            passages.append(Passage(
                chunk_id=chunk_id, text=text or "",
                source=str(meta.get("source", "unknown")),
                section=str(meta.get("section", "")),
                page=int(page) if isinstance(page, (int, float)) else None,
                score=round(1.0 - float(distance), 4),
            ))
        log.debug("retrieved", extra={"history": where is None, "n": len(passages)})
        return passages

    # ------------------------------------------------------------------ readiness
    def readiness_checks(self) -> dict[str, dict[str, Any]]:
        """The readiness checks this retriever owns: chroma, collection, embed_pin."""
        checks: dict[str, dict[str, Any]] = {}
        url = f"http://{self.settings.chroma_host}:{self.settings.chroma_port}/api/v2/heartbeat"
        try:
            httpx.get(url, timeout=2.0).raise_for_status()
            checks["chroma"] = {"ok": True}
        except httpx.HTTPError as exc:
            checks["chroma"] = {"ok": False, "error": f"heartbeat failed: {exc}"}
            checks["collection"] = {"ok": False, "error": "skipped: chroma unreachable"}
            checks["embed_pin"] = {"ok": False, "error": "skipped: chroma unreachable"}
            return checks
        try:
            count = int(self.collection(refresh=True).count())
        except Exception as exc:
            checks["collection"] = {"ok": False, "error": str(exc)}
            checks["embed_pin"] = {"ok": False, "error": "skipped: no collection"}
            return checks
        checks["collection"] = ({"ok": True, "count": count} if count > 0 else
                                {"ok": False, "error": "collection is empty", "count": 0})
        problem = pin_problem(self._meta, self.settings.embed_model)
        checks["embed_pin"] = ({"ok": True, "embed_model": self.settings.embed_model}
                               if problem is None else {"ok": False, "error": problem})
        return checks
