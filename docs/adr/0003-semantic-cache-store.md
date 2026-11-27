# ADR 0003: Semantic cache in Redis, keyed by index generation

## Status
Accepted · 8 Nov 2026

## Context
Students ask the same handful of questions in many wordings, and each answer costs an
LLM call of several seconds. The API runs as three to eight replicas, so a cache inside
one process would hit for roughly a third of repeats at best, and would be lost on every
rollout. A cached answer must also never outlive the index it was built from.

## Decision
- **Store:** one Redis Deployment (`k8s/base/redis.yaml`) with a 1 Gi PVC `cache-data`
  and append-only-file persistence (fsync every second), `maxmemory 200mb` with LRU
  eviction, ClusterIP only. All API replicas share it, and a restarted Redis pod keeps
  its entries. `strategy: Recreate`, because the ReadWriteOnce volume can be mounted by
  one pod at a time.
- **Match:** look up the exact normalised question first. Semantic reuse requires both
  questions to match the same explicitly recognised intent and their canonical embeddings
  to meet `CACHE_THRESHOLD` (0.95). Currently only reviewed phrasings of the unqualified
  academic-warning CGPA threshold qualify. Questions with other wording or qualifiers
  use exact matching. The scan is bounded by `CACHE_MAX_ENTRIES`.
- **Invalidation:** every key contains a generation hash of the collection's `index_id`,
  the embedding model, the LLM provider and model, the prompt version, the relevance
  floor and the cache policy version. A re-index or policy version change isolates the
  old answers immediately, and they expire by TTL (24 h).
- **Never cached:** abstentions, fallbacks, answers without a valid citation, follow-up
  questions.
- **Contract change:** `AskResponse` gained `cached: bool = False`, and the route sets
  `X-Cache: hit|miss`. The field is optional with a default, so the UI, `run_eval.py` and
  every existing test still validate the response unchanged.
- **Failure:** `/readyz` reports Redis as a failed dependency (the brief requires it).
  `/api/ask` itself logs and skips the cache when Redis errors.

## Options considered
- **In-process dictionary:** no dependency, but per-replica and lost on restart. Kept as
  `CACHE_BACKEND=memory` for `make dev` and tests only.
- **Chroma as the cache** (a second collection of question vectors): one store fewer, and
  real nearest-neighbour search. Rejected: it mixes a disposable cache into the store we
  protect most, and has no TTL.
- **Exact-match only:** no false hits, but almost no hits either, because students
  rephrase.

## Consequences
- Easier: repeated questions answer in milliseconds without the model; a re-index can
  never serve answers from an old index.
- Harder: the linear scan costs O(entries) cosine computations per miss, which is fine for
  hundreds of entries and wrong for millions (then use a vector index). Making readiness
  depend on Redis means a Redis outage takes every API pod out of the Service, even though
  the API could answer without its cache. We follow the brief here, but would argue for
  reporting Redis without failing readiness.
- Watch: `rahbar_cache_total{result="hit"}` against `miss`, and false hits found in the
  threshold experiment (`docs/PIPELINE.md`, "Semantic cache").

## Original cosine-only measurement (8 Nov 2026)
Measured with the real model, not chosen by feel: `eval/cache_threshold.py` over 25
labelled pairs, `nomic-embed-text`, a real Redis (workflow `cache-threshold`, run
42517013627). Results and method are in `docs/PIPELINE.md`, "Semantic cache".

- Zero false hits from 0.94 upward; the first false hit appears at 0.93 (Dean's List vs
  Rector's List, similarity 0.933).
- **0.95 is kept:** identical measured outcome to 0.94, with a wider margin over 0.933.
- **Consequence for this design:** at a safe threshold the semantic part of the cache adds
  nothing on this test set. All 8 paraphrases miss, and only exact or normalised repeats
  hit. With `nomic-embed-text`, paraphrases (0.735 to 0.939) and near misses (up to 0.933)
  overlap. The later above/below regression showed that questions with different answers
  can also score well above 0.95, so semantic reuse now requires a recognised intent.

## Guarded measurement (25 Nov 2026)

The expanded set has 32 labelled pairs, including two follow-ups tested for cache bypass.
At 0.95, cosine-only matching produced 3 correct hits, 1 false hit and 9 misses among
equivalent pairs. Guarded matching produced 5 correct hits, 0 false hits and 7 misses
among equivalent pairs. Redis agreed with all 30 measured lookup decisions and a new
generation missed the old entry. The above/below pair scored 0.995664. The original
academic-warning paraphrases scored 0.851503 before their reviewed intent was canonicalised.
