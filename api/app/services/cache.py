"""Semantic cache (section 3.3, ADR 0003).

A hit is a previously answered question whose embedding has cosine similarity of at
least CACHE_THRESHOLD with the new one. The exact same question (after normalising
case and spacing) is found first by key, without a scan.

Invalidation: every entry lives under a *generation*, made of the index id the indexer
wrote into the collection metadata, the embedding model, the LLM model and the prompt
version. A re-index, a model change or a prompt change starts an empty generation;
the old one simply expires (CACHE_TTL), nothing has to be flushed by hand.

Never cached (`should_cache`): abstentions, fallbacks, answers without a citation, and
follow-up questions that only make sense after an earlier turn ("what about MS?").

Redis is the store on the cluster, so all API replicas share one cache. A Redis
outage makes /readyz fail (section 4.3); /api/ask itself degrades to no cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import Settings
from ..embeddings import cosine
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")

_FOLLOWUP = re.compile(
    r"^\s*(and|also|but|so|then|what about|how about|same for|and for|what if)\b"
    r"|\b(it|that|this|those|these|they|them|above|previous answer|you said)\s*\??\s*$",
    re.I)


def is_followup(question: str) -> bool:
    """A question that depends on an earlier turn; its answer must never be reused."""
    return bool(_FOLLOWUP.search(question.strip()))


def normalise_question(question: str) -> str:
    return " ".join(question.lower().split()).rstrip("?.! ")


def should_cache(question: str, response: AskResponse) -> bool:
    if not response.answered_from_context or not response.citations:
        return False                 # never cache an abstention or a fallback
    return not is_followup(question)


@dataclass
class CacheHit:
    response: AskResponse
    similarity: float


class SemanticCache(Protocol):
    name: str

    def lookup(self, question: str, vector: list[float], generation: str) -> CacheHit | None:
        ...

    def store(self, question: str, vector: list[float], generation: str,
              response: AskResponse) -> None:
        ...

    def ping(self) -> bool:
        ...


def _key(question: str) -> str:
    return hashlib.sha256(normalise_question(question).encode()).hexdigest()[:24]


class NullCache:
    name = "none"

    def lookup(self, question: str, vector: list[float], generation: str) -> CacheHit | None:
        return None

    def store(self, question: str, vector: list[float], generation: str,
              response: AskResponse) -> None:
        return None

    def ping(self) -> bool:
        return True


class MemoryCache:
    """One process only: for `make dev` and the tests. On the cluster use Redis."""

    name = "memory"

    def __init__(self, threshold: float, ttl: int, max_entries: int) -> None:
        self.threshold, self.ttl, self.max_entries = threshold, ttl, max_entries
        self.entries: dict[str, dict[str, tuple[float, list[float], str]]] = {}

    def lookup(self, question: str, vector: list[float], generation: str) -> CacheHit | None:
        now = time.time()
        bucket = self.entries.get(generation, {})
        for k in [k for k, (exp, _, _) in bucket.items() if exp < now]:
            del bucket[k]
        exact = bucket.get(_key(question))
        if exact:
            return CacheHit(AskResponse.model_validate_json(exact[2]), 1.0)
        best: tuple[float, str] | None = None
        for _, vec, payload in bucket.values():
            sim = cosine(vector, vec)
            if sim >= self.threshold and (best is None or sim > best[0]):
                best = (sim, payload)
        if best:
            return CacheHit(AskResponse.model_validate_json(best[1]), round(best[0], 4))
        return None

    def store(self, question: str, vector: list[float], generation: str,
              response: AskResponse) -> None:
        bucket = self.entries.setdefault(generation, {})
        if len(bucket) >= self.max_entries:
            bucket.pop(next(iter(bucket)))
        bucket[_key(question)] = (time.time() + self.ttl, vector, response.model_dump_json())

    def ping(self) -> bool:
        return True


class RedisCache:
    """Entries are hashes ``rahbar:cache:<generation>:<key>`` with fields ``vec`` and
    ``resp``, each with a TTL; a per-generation sorted set indexes them by time so the
    scan is bounded to the newest CACHE_MAX_ENTRIES."""

    name = "redis"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(settings.redis_url, socket_timeout=1.0,
                                          socket_connect_timeout=1.0)
        self.r = client
        self.threshold = settings.cache_threshold
        self.ttl = settings.cache_ttl
        self.max_entries = settings.cache_max_entries

    def _entry(self, generation: str, key: str) -> str:
        return f"rahbar:cache:{generation}:{key}"

    def _index(self, generation: str) -> str:
        return f"rahbar:cache:{generation}:index"

    def lookup(self, question: str, vector: list[float], generation: str) -> CacheHit | None:
        exact = self.r.hget(self._entry(generation, _key(question)), "resp")
        if exact:
            return CacheHit(AskResponse.model_validate_json(exact), 1.0)
        keys = self.r.zrevrange(self._index(generation), 0, self.max_entries - 1)
        if not keys:
            return None
        pipe = self.r.pipeline()
        for k in keys:
            pipe.hget(self._entry(generation, k.decode() if isinstance(k, bytes) else k), "vec")
        best: tuple[float, str] | None = None
        for k, raw in zip(keys, pipe.execute()):
            if raw is None:
                continue
            sim = cosine(vector, json.loads(raw))
            if sim >= self.threshold and (best is None or sim > best[0]):
                best = (sim, k.decode() if isinstance(k, bytes) else k)
        if best is None:
            return None
        resp = self.r.hget(self._entry(generation, best[1]), "resp")
        return CacheHit(AskResponse.model_validate_json(resp), round(best[0], 4)) if resp else None

    def store(self, question: str, vector: list[float], generation: str,
              response: AskResponse) -> None:
        key = _key(question)
        entry, index = self._entry(generation, key), self._index(generation)
        pipe = self.r.pipeline()
        pipe.hset(entry, mapping={"vec": json.dumps(vector), "resp": response.model_dump_json()})
        pipe.expire(entry, self.ttl)
        pipe.zadd(index, {key: time.time()})
        pipe.zremrangebyrank(index, 0, -self.max_entries - 1)   # keep the newest only
        pipe.expire(index, self.ttl)
        pipe.execute()

    def ping(self) -> bool:
        return bool(self.r.ping())


def get_cache(settings: Settings) -> SemanticCache:
    if settings.cache_backend == "redis":
        return RedisCache(settings)
    if settings.cache_backend == "memory":
        return MemoryCache(settings.cache_threshold, settings.cache_ttl,
                           settings.cache_max_entries)
    return NullCache()
