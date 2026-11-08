"""Turns a question into an AskResponse. This is where the query pipeline lives.

    1. semantic cache lookup (services/cache.py)          - skipped for follow-ups
    2. retrieve top-k, superseded filtered out (retrieval/)
    3. relevance floor: nothing relevant -> decline without calling the model
    4. generate with the Answer schema, one retry on invalid output (providers/llm/)
    5. safe fallback when generation fails or times out
    6. citation check: every citation must name a passage the model was given
    7. cache the answer, unless it is an abstention, a fallback or a follow-up
    8. one structured log line with the ids, scores and outcome

Each step lives in its own module so it can be unit-tested; this function only
sequences them. Dependency outages (Chroma, embeddings) propagate as
DependencyUnavailable and become a 503 in main.py.
"""

from __future__ import annotations

import hashlib
import logging
import time

from ..config import Settings
from ..embeddings import Embedder, HashEmbedder
from ..errors import DependencyUnavailable
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.prompt import PROMPT_VERSION
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from .cache import NullCache, SemanticCache, is_followup, should_cache
from .grounding import abstention, apply_relevance_floor, check_citations, fallback

log = logging.getLogger("rahbar.answer")

_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def cache_generation(retriever: Retriever, provider: LLMProvider, settings: Settings) -> str:
    """Everything that changes an answer. A new value is an empty cache (invalidation)."""
    index_id = getattr(retriever, "index_id", lambda: retriever.name)()
    raw = "|".join([retriever.name, str(index_id), settings.embed_model, provider.name,
                    settings.llm_model, PROMPT_VERSION, str(settings.relevance_floor)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _generate(question: str, passages: list[Passage],
              provider: LLMProvider) -> tuple[Answer, str | None]:
    """The model's Answer, or the safe fallback and the reason it was needed."""
    started = time.perf_counter()
    try:
        return provider.generate(question, passages), None
    except Exception as exc:   # invalid twice, timeout, HTTP error: never a 500
        reason = type(exc).__name__
        log.warning("generation failed; using fallback",
                    extra={"reason": reason, "error": str(exc)[:300]})
        metrics.FALLBACKS.labels(reason=reason).inc()
        return fallback(reason), reason
    finally:
        metrics.GENERATION_SECONDS.observe(time.perf_counter() - started)


def _ground(answer: Answer, passages: list[Passage]) -> tuple[Answer, str | None]:
    """Apply the citation check. Returns the answer to send and an abstention reason."""
    if not answer.answered_from_context:
        metrics.ABSTENTIONS.labels(reason="model").inc()
        return answer.model_copy(update={"citations": []}), "model"
    check = check_citations(answer.citations, passages)
    if check.dropped:
        metrics.CITATIONS_DROPPED.inc(check.dropped)
    if not check.valid:
        metrics.ABSTENTIONS.labels(reason="no_valid_citation").inc()
        return abstention("the answer cited no retrieved passage"), "no_valid_citation"
    confidence = _DOWNGRADE[answer.confidence] if check.dropped else answer.confidence
    return answer.model_copy(update={"citations": check.valid, "confidence": confidence}), None


def answer_question(
    question: str, retriever: Retriever, provider: LLMProvider, settings: Settings,
    cache: SemanticCache | None = None, embedder: Embedder | None = None,
) -> AskResponse:
    rid = request_id_var.get()
    cache = cache or NullCache()
    embedder = embedder or HashEmbedder()
    followup = is_followup(question)
    generation = cache_generation(retriever, provider, settings)

    # 1. cache
    vector: list[float] = []
    if cache.name != "none" and not followup:
        try:
            vector = embedder.embed(question)
            hit = cache.lookup(question, vector, generation)
        except DependencyUnavailable:
            raise
        except Exception as exc:          # a cache outage must not fail the request
            log.warning("cache lookup failed", extra={"error": str(exc)[:200]})
            metrics.CACHE.labels(result="error").inc()
            hit = None
        if hit is not None:
            metrics.CACHE.labels(result="hit").inc()
            log.info("answered", extra={"cache": "hit", "similarity": hit.similarity})
            return hit.response.model_copy(update={"request_id": rid, "cached": True})
        metrics.CACHE.labels(result="miss").inc()

    # 2. retrieve
    started = time.perf_counter()
    passages = retriever.retrieve(question, k=settings.top_k)
    metrics.RETRIEVAL_SECONDS.observe(time.perf_counter() - started)
    if passages:
        metrics.TOP_SCORE.observe(passages[0].score)

    # 3. relevance floor
    grounded = apply_relevance_floor(passages, settings.relevance_floor)
    fallback_reason: str | None = None
    declined: str | None
    if not grounded:
        metrics.ABSTENTIONS.labels(reason="relevance_floor").inc()
        answer, declined = abstention("no passage above the relevance floor"), "relevance_floor"
    else:
        # 4-6. generate, fall back, check citations
        answer, fallback_reason = _generate(question, grounded, provider)
        declined = fallback_reason
        if fallback_reason is None:
            answer, declined = _ground(answer, grounded)

    response = AskResponse(
        request_id=rid,
        answer=answer.answer,
        citations=answer.citations,
        answered_from_context=answer.answered_from_context,
        confidence=answer.confidence,
        retrieved=[
            RetrievedRef(
                chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page, score=p.score
            )
            for p in passages
        ],
        provider=provider.name,
    )

    # 7. cache
    if cache.name != "none" and fallback_reason is None and should_cache(question, response):
        try:
            cache.store(question, vector or embedder.embed(question), generation, response)
        except Exception as exc:
            log.warning("cache store failed", extra={"error": str(exc)[:200]})

    # 8. log
    log.info(
        "answered",
        extra={
            "cache": "skip" if followup or cache.name == "none" else "miss",
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "above_floor": len(grounded),
            "answered_from_context": response.answered_from_context,
            "declined": declined,
            "citations": len(response.citations),
        },
    )
    return response
