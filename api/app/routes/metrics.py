"""Prometheus metrics at /metrics (section 4.8).

Request count and latency come with the kit. The rest describe the RAG pipeline:
where the time goes (retrieval, generation), and how often the system says no.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
RETRIEVAL_SECONDS = Histogram(
    "rahbar_retrieval_seconds", "Embedding plus vector search",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
GENERATION_SECONDS = Histogram(
    "rahbar_generation_seconds", "LLM call including the retry",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120))
TOP_SCORE = Histogram(
    "rahbar_retrieval_top_score", "Similarity of the best retrieved passage",
    buckets=(0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0))
CACHE = Counter("rahbar_cache_total", "Semantic cache lookups", ["result"])   # hit|miss|error
ABSTENTIONS = Counter("rahbar_abstentions_total", "Declined answers", ["reason"])
FALLBACKS = Counter("rahbar_fallbacks_total", "Generation failures answered safely", ["reason"])
CITATIONS_DROPPED = Counter("rahbar_citations_dropped_total",
                            "Citations that matched no retrieved passage")
# Tokens as reported by the provider, per LLM call (retries included). Labels stay
# low-cardinality: provider and model come from the ConfigMap, direction is input|output.
LLM_TOKENS = Counter("rahbar_llm_tokens_total", "LLM tokens reported by the provider",
                     ["provider", "model", "direction"])
LLM_USAGE_MISSING = Counter("rahbar_llm_usage_missing_total",
                            "LLM calls whose reply did not report token usage",
                            ["provider", "model"])
TOKENS_PER_REQUEST = Histogram(
    "rahbar_llm_tokens_per_request", "Reported LLM tokens per answered request",
    ["direction"], buckets=(100, 250, 500, 1000, 2000, 4000, 8000, 16000))


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
