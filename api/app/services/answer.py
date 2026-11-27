"""Turns a question into an AskResponse. This is where the query pipeline lives.

    1. semantic cache lookup (services/cache.py)          - skipped for follow-ups
    2. retrieve top-k, superseded filtered out (retrieval/)
    3. relevance floor and lexical support: nothing relevant, or nothing that mentions
       what the question is about -> decline without calling the model
    4. generate with the Answer schema, one retry on invalid output (providers/llm/)
    5. safe fallback when generation fails or times out
    6. citation check: every citation must name a passage the model was given
    7. cache the answer, unless it is an abstention, a fallback or a follow-up
    8. one structured log line with the ids, scores and outcome, and one trace record
       (services/trace.py, Assignment 03 Part D1) with everything needed to explain it

Each step lives in its own module so it can be unit-tested; this function only
sequences them. Dependency outages (Chroma, embeddings) propagate as
DependencyUnavailable and become a 503 in main.py; their trace records the failure.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Any, Literal

from ..config import Settings
from ..embeddings import Embedder, HashEmbedder, query_prefix
from ..errors import DependencyUnavailable
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..providers.llm.prompt import PROMPT_VERSION
from ..providers.llm.usage import TokenUsage, usage_var
from ..retrieval.base import Retriever
from ..routes import metrics
from ..schemas import Answer, AskResponse, Passage, RetrievedRef
from . import guardrails
from . import trace as tracing
from .cache import (SEMANTIC_CACHE_POLICY, NullCache, SemanticCache, is_followup,
                    semantic_cache_intent, should_cache)
from .grounding import (abstention, apply_relevance_floor, check_citations, fallback,
                        has_lexical_support, render_inline_citations)

log = logging.getLogger("rahbar.answer")

_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def cache_generation(retriever: Retriever, provider: LLMProvider, settings: Settings) -> str:
    """Everything that changes an answer. A new value is an empty cache (invalidation)."""
    index_id = getattr(retriever, "index_id", lambda: retriever.name)()
    trace = tracing.current()
    if trace is not None:
        trace.update("collection", index_id=str(index_id))
    raw = "|".join([retriever.name, str(index_id), settings.embed_model, provider.name,
                    settings.llm_model, PROMPT_VERSION, str(settings.relevance_floor),
                    SEMANTIC_CACHE_POLICY])
    if provider.name == "hosted":
        raw += f"|{settings.hosted_base_url.rstrip('/')}|{settings.hosted_json_mode}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def generation_parameters(provider: LLMProvider, settings: Settings) -> dict[str, Any]:
    """The model and the parameters it is called with. Never the API key."""
    if "ollama" in provider.name:
        return {"model": settings.llm_model, "temperature": settings.temperature,
                "output_format": "Answer JSON schema (Ollama structured output)",
                "num_thread": settings.ollama_num_threads or "Ollama default",
                "timeout_s": settings.llm_timeout, "seed": "not set"}
    if provider.name == "hosted":
        return {"model": settings.llm_model, "temperature": settings.temperature,
                "response_format": settings.hosted_json_mode,
                "endpoint": settings.hosted_base_url, "timeout_s": settings.llm_timeout,
                "seed": "not set"}
    return {"note": f"provider {provider.name!r} calls no model"}


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
        trace = tracing.current()
        if trace is not None:
            trace.guardrail("generation_fallback", "fallback_answer", reason=reason,
                            error=str(exc)[:300])
        return fallback(reason), reason
    finally:
        metrics.GENERATION_SECONDS.observe(time.perf_counter() - started)


def _ground(answer: Answer, passages: list[Passage]) -> tuple[Answer, str | None]:
    """Apply the citation check. Returns the answer to send and an abstention reason."""
    trace = tracing.current()
    if not answer.answered_from_context:
        metrics.ABSTENTIONS.labels(reason="model").inc()
        return answer.model_copy(update={"citations": []}), "model"
    check = check_citations(answer.citations, passages)
    if check.dropped:
        metrics.CITATIONS_DROPPED.inc(check.dropped)
        if trace is not None:
            trace.guardrail("citation_check", "dropped_citations", dropped=check.dropped,
                            rejected=check.rejected[:5])
    if check.dropped or not check.valid:
        # what the model cited versus what it was given: tells a fabricated citation
        # from a real passage cited in another form
        log.info("citations rejected", extra={
            "rejected": check.rejected[:5], "cited": len(answer.citations),
            "offered": [f"{p.source[:60]} | {p.section[:60]}" for p in passages][:5]})
    if not check.valid:
        metrics.ABSTENTIONS.labels(reason="no_valid_citation").inc()
        if trace is not None:
            trace.guardrail("citation_check", "replaced_with_abstention",
                            reason="the answer cited no retrieved passage")
        return abstention("the answer cited no retrieved passage"), "no_valid_citation"
    cited_answer = render_inline_citations(answer.answer, check, passages)
    if cited_answer is None:
        metrics.ABSTENTIONS.labels(reason="no_valid_citation").inc()
        log.info("inline citations rejected")
        if trace is not None:
            trace.guardrail("citation_check", "replaced_with_abstention",
                            reason="the answer lacked verified sentence citations")
        return abstention("the answer lacked verified sentence citations"), "no_valid_citation"
    confidence = _DOWNGRADE[answer.confidence] if check.dropped else answer.confidence
    return answer.model_copy(update={"answer": cited_answer, "citations": check.valid,
                                     "confidence": confidence}), None


def answer_question(
    question: str, retriever: Retriever, provider: LLMProvider, settings: Settings,
    cache: SemanticCache | None = None, embedder: Embedder | None = None,
) -> AskResponse:
    # Token usage for this request only: providers add to it on every LLM call.
    usage = TokenUsage()
    token = usage_var.set(usage)
    rid = request_id_var.get()
    trace = tracing.Trace(rid if rid != "-" else uuid.uuid4().hex[:16], question)
    trace_token = tracing.trace_var.set(trace)
    trace.set("pipeline", {"retriever": retriever.name, "provider": provider.name,
                           "top_k": settings.top_k, "relevance_floor": settings.relevance_floor,
                           "prompt_version": PROMPT_VERSION,
                           "cache_backend": (cache or NullCache()).name})
    trace.set("embedding", {"model": settings.embed_model,
                            "query_prefix": query_prefix(settings.embed_model),
                            "model_digest": "not read: no query was embedded for retrieval "
                                           "(cache hit, or a retriever without a digest)"})
    trace.update("collection", name=settings.collection if retriever.name == "chroma"
                 else f"{retriever.name} retriever (no collection)")
    status = "ok"
    try:
        return _answer_question(question, retriever, provider, settings, cache, embedder,
                                usage, trace)
    except Exception as exc:
        status = "failed"
        trace.set("outcome", "error")
        trace.set("error", f"{type(exc).__name__}: {str(exc)[:500]}")
        raise
    finally:
        trace.update("generation", tokens=usage.log_fields())
        try:
            trace.finish(status, settings.trace_dir, settings.trace_stdout)
        except Exception as exc:       # a full disk must not fail the request
            log.warning("trace write failed", extra={"error": str(exc)[:200]})
        tracing.trace_var.reset(trace_token)
        usage_var.reset(token)


def _response_record(response: AskResponse, answer: Answer | None) -> dict[str, Any]:
    record = {"answer": response.answer, "answered_from_context": response.answered_from_context,
              "confidence": response.confidence, "outcome": response.outcome,
              "cached": response.cached, "provider": response.provider}
    if answer is not None:
        record["model_reasoning"] = answer.reasoning      # never shown to the user
    return record


def _answer_question(
    question: str, retriever: Retriever, provider: LLMProvider, settings: Settings,
    cache: SemanticCache | None, embedder: Embedder | None, usage: TokenUsage,
    trace: tracing.Trace,
) -> AskResponse:
    rid = request_id_var.get()
    cache = cache or NullCache()
    embedder = embedder or HashEmbedder()
    followup = is_followup(question)
    generation = cache_generation(retriever, provider, settings)
    cache_text = semantic_cache_intent(question) or question

    injected = guardrails.scan_injection(question)
    if injected:
        trace.guardrail("input_injection_scan", "flagged", where="question", matches=injected,
                        handling="answered only from the passages (system rule 1)")

    # 1. cache
    vector: list[float] = []
    if cache.name != "none" and not followup:
        trace.update("cache", generation=generation,
                     **({"intent_key": cache_text} if cache_text != question else {}))
        with trace.stage("cache_lookup"):
            try:
                vector = embedder.embed(cache_text)
                hit = cache.lookup(question, vector, generation)
            except DependencyUnavailable:
                raise
            except Exception as exc:          # a cache outage must not fail the request
                log.warning("cache lookup failed", extra={"error": str(exc)[:200]})
                metrics.CACHE.labels(result="error").inc()
                trace.update("cache", result="error", error=str(exc)[:200])
                hit = None
        if hit is not None:
            metrics.CACHE.labels(result="hit").inc()
            log.info("answered", extra={"cache": "hit", "similarity": hit.similarity,
                                        **usage.log_fields()})
            # Provenance, not a re-enactment: nothing was retrieved or generated now.
            trace.update("cache", result="hit", similarity=round(hit.similarity, 4),
                         served_from_trace_id=hit.response.request_id)
            trace.set("retrieval", {
                "status": "skipped", "reason": "served from the cache",
                "cached_retrieved": [r.model_dump() for r in hit.response.retrieved],
                "note": "recorded by the trace in cache.served_from_trace_id"})
            trace.set("generation", {"status": "skipped", "reason": "served from the cache"})
            for stage in ("retrieval", "relevance_floor", "generation", "citation_check",
                          "cache_store"):
                trace.skip(stage, "served from the cache")
            response = hit.response.model_copy(update={"request_id": rid, "cached": True})
            trace.set("response", _response_record(response, None))
            trace.set("citations", [c.model_dump() for c in response.citations])
            trace.set("outcome", response.outcome)
            return response
        metrics.CACHE.labels(result="miss").inc()
        trace.update("cache", result=trace.record["cache"].get("result", "miss"))
    else:
        reason = "follow-up question: never cached" if followup else "cache disabled"
        trace.update("cache", result="skip", reason=reason)
        trace.skip("cache_lookup", reason)

    # 2. retrieve
    trace.set("retrieval", {"status": "ran", "k": settings.top_k})
    started = time.perf_counter()
    with trace.stage("retrieval", note="includes embedding the question"):
        passages = retriever.retrieve(question, k=settings.top_k)
    metrics.RETRIEVAL_SECONDS.observe(time.perf_counter() - started)
    if passages:
        metrics.TOP_SCORE.observe(passages[0].score)

    # 3. relevance floor
    with trace.stage("relevance_floor"):
        grounded = apply_relevance_floor(passages, settings.relevance_floor)
    sent = {p.chunk_id for p in grounded}
    trace.update("retrieval", results=[{
        "rank": i, "chunk_id": p.chunk_id, "doc_id": p.doc_id or p.source,
        "source": p.source, "doc_version": p.doc_version, "superseded": p.superseded,
        "section": p.section, "page": p.page, "score": p.score,
        "above_floor": p.chunk_id in sent, "text_preview": p.text[:200]}
        for i, p in enumerate(passages, start=1)])
    trace.record["retrieval"].setdefault("filter", f"not reported by the {retriever.name} retriever")
    trace.set("rerank", {"applied": False, "note": "no reranker: passages keep retrieval order",
                         "order_sent_to_model": [p.chunk_id for p in grounded]})
    if len(grounded) < len(passages):
        trace.guardrail("relevance_floor", "filtered", floor=settings.relevance_floor,
                        removed=[p.chunk_id for p in passages if p.chunk_id not in sent])
    flagged: dict[str, str] = {}
    for p in passages:
        matches = guardrails.scan_injection(p.text)
        if matches:
            flagged[p.chunk_id] = guardrails.injected_sentence(p.text, matches[0])
            trace.guardrail(
                "passage_injection_scan", "flagged", chunk_id=p.chunk_id, source=p.source,
                matches=matches, sent_to_model=p.chunk_id in sent,
                handling=("given to the model as untrusted <passage> data; the system message "
                          "forbids following it" if p.chunk_id in sent
                          else "below the relevance floor: not given to the model"))
        tags = guardrails.neutralised_tags(p.text)
        if tags and p.chunk_id in sent:
            trace.guardrail("passage_tag_neutraliser", "neutralised", chunk_id=p.chunk_id,
                            tags=tags)

    fallback_reason: str | None = None
    declined: str | None
    model_answer: Answer | None = None
    if not grounded:
        metrics.ABSTENTIONS.labels(reason="relevance_floor").inc()
        answer, declined = abstention("no passage above the relevance floor"), "relevance_floor"
        trace.guardrail("relevance_floor", "declined", reason="no passage above the floor")
    elif not has_lexical_support(question, grounded):
        metrics.ABSTENTIONS.labels(reason="no_lexical_support").inc()
        answer = abstention("no passage mentions anything the question is about")
        declined = "no_lexical_support"
        trace.guardrail("lexical_support", "declined",
                        reason="no passage mentions anything the question is about")
    else:
        # 4-6. generate, fall back, check citations
        trace.set("generation", {"status": "ran", "provider": provider.name,
                                 "parameters": generation_parameters(provider, settings)})
        with trace.stage("generation"):
            model_answer, fallback_reason = _generate(question, grounded, provider)
        answer = model_answer
        declined = fallback_reason
        if usage.input_reported:
            metrics.TOKENS_PER_REQUEST.labels(direction="input").observe(usage.input_tokens)
        if usage.output_reported:
            metrics.TOKENS_PER_REQUEST.labels(direction="output").observe(usage.output_tokens)
        if fallback_reason is None:
            with trace.stage("citation_check"):
                answer, declined = _ground(answer, grounded)
        else:
            trace.update("generation", status="failed", fallback_reason=fallback_reason)
            trace.skip("citation_check", "generation failed: fallback answer")
    if declined in ("relevance_floor", "no_lexical_support"):
        trace.set("generation", {"status": "skipped", "reason": f"declined before generation: {declined}"})
        trace.skip("generation", f"declined before generation: {declined}")
        trace.skip("citation_check", "no model answer to check")
    if trace.record["prompt"] is None and trace.record["generation"].get("status") == "ran":
        trace.update("generation", prompt_note=f"provider {provider.name!r} builds no prompt")

    outcome: Literal["answered", "abstained", "generation_failed", "citation_failed"]
    if fallback_reason is not None:
        outcome = "generation_failed"
    elif declined == "no_valid_citation":
        outcome = "citation_failed"
    elif answer.answered_from_context:
        outcome = "answered"
    else:
        outcome = "abstained"

    for chunk_id, sentence in flagged.items():
        if chunk_id in sent:
            echoed = guardrails.echoes(answer.answer, sentence)
            trace.guardrail("output_injection_echo_check",
                            "echo_detected" if echoed else "passed", chunk_id=chunk_id,
                            injected_sentence=sentence[:300])

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
        outcome=outcome,
    )
    trace.set("response", _response_record(response, model_answer))
    trace.set("citations", [c.model_dump() for c in response.citations])
    trace.set("outcome", outcome)
    if not response.answered_from_context:
        trace.set("abstention", {"reason": declined})

    # 7. cache
    if cache.name != "none" and fallback_reason is None and should_cache(question, response):
        with trace.stage("cache_store"):
            try:
                cache.store(question, vector or embedder.embed(cache_text), generation, response)
            except Exception as exc:
                log.warning("cache store failed", extra={"error": str(exc)[:200]})
                trace.update("cache", store_error=str(exc)[:200])
    else:
        trace.skip("cache_store", "cache disabled" if cache.name == "none" else
                   "not cacheable: abstention, fallback, follow-up or no valid citation")

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
            **usage.log_fields(),
        },
    )
    return response
