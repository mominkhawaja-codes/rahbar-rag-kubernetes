# ADR 0001: The indexer runs as a Kubernetes Job, and swaps in a complete index

## Status
Accepted · 8 Nov 2026

## Context
The index has to be built from about 280 pages, two of the documents need OCR, and every
chunk needs an embedding from a model on another machine. The API runs as three replicas
behind an HPA that can take it to eight, and Chroma keeps its data on one ReadWriteOnce
volume. Something must write the index, and the API must keep answering while that
happens.

## Decision
- Indexing is a **Job** (`k8s/base/indexer-job.yaml`) plus a **CronJob** `reindex` for
  scheduled and on-demand runs (`make index`). It runs once, exits 0 on success, and
  Kubernetes retries a failure up to `backoffLimit: 3`. `concurrencyPolicy: Forbid` stops
  two indexers overlapping.
- The API **never writes** (`api/app/main.py` only reads; the checker's "no indexing in
  startup" rule passes).
- `indexer/pipeline/store.py` builds the whole new index in `rahbar__staging`, checks its
  count, then renames live → `rahbar__previous`, staging → `rahbar`, and drops
  `__previous`. Until that swap, the API serves the old index untouched. A failure at any
  earlier point leaves the live collection as it was.
- The new collection records `embed_model`, `dimension` and a fresh `index_id`. The API
  checks the first two (the pin) and uses the third to invalidate its cache.

## Options considered
- **Index on API start-up.** With three replicas this means three writers racing on one
  collection, a full re-index on every restart or scale-up, and a probe that cannot tell
  "indexing" from "hung". Rejected: it is the failure section 7 asks about.
- **Delete and rebuild the live collection in place.** Simpler, but for the minutes the
  build takes, the API answers from an empty or half-written index, and readiness would
  have to fail cluster-wide. Rejected.
- **Write a new collection per build and point the API at it through the ConfigMap.**
  No rename window, but every re-index then needs an API rollout. Rejected for this size;
  it would be the choice if the brief rename window ever mattered.

## Consequences
- Easier: the index is reproducible from an image tag (the corpus is baked into the
  indexer image, see `docs/PIPELINE.md`), and indexing failures are visible as failed Jobs
  with logs.
- Harder: between the two renames there is a moment, measured in milliseconds, when no
  collection is named `rahbar`. A request landing then gets a 503, and readiness may flap
  once. The swap also needs room for two copies of the index in Chroma's volume (2 Gi is
  far more than the index needs).
- Watch: Job duration and peak memory (OCR at 300 dpi) against the 1536 Mi limit; an
  OOMKilled indexer shows exit code 137 (`docs/ENGINEERING-NOTES.md`, question 4).
