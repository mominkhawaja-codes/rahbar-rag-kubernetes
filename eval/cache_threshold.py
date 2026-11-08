#!/usr/bin/env python3
"""Measure the semantic-cache threshold with the real embedding model (ADR 0003).

Needs a running Ollama with nomic-embed-text and a running Redis:

    OLLAMA_URL=http://localhost:11434 REDIS_URL=redis://localhost:6379/0 \
        python eval/cache_threshold.py --out cache-threshold.json

1. Embeds every question with the API's own OllamaEmbedder (same model, same
   "search_query:" prefix as production) and computes cosine similarity per pair.
2. For each threshold counts true hits, false hits and misses; picks the lowest threshold
   with zero false hits (the lecture's rule).
3. Replays the lookups through a real RedisCache at the chosen threshold and at 0.95, and
   checks that abstentions and follow-ups are never cached and that a new index
   generation misses.

Exit code 0 when every check passes; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.config import Settings  # noqa: E402
from app.embeddings import OllamaEmbedder, cosine  # noqa: E402
from app.schemas import AskResponse, Citation, RetrievedRef  # noqa: E402
from app.services.cache import RedisCache, is_followup, should_cache  # noqa: E402

THRESHOLDS = [0.80, 0.85, 0.88, 0.90, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]


def response(question: str, answered: bool = True) -> AskResponse:
    ref = RetrievedRef(chunk_id="c1", source="doc.pdf", section="S [1.1]", page=1, score=0.8)
    return AskResponse(request_id="exp", answer=f"answer to: {question}",
                       citations=[Citation(source="doc.pdf", section="S [1.1]", page=1)]
                       if answered else [],
                       answered_from_context=answered, confidence="high" if answered else "low",
                       retrieved=[ref], provider="experiment")


def score(pairs: list[dict], t: float) -> dict:
    measured = [p for p in pairs if p["kind"] != "followup"]
    true_hits = sum(1 for p in measured if p["same_answer"] and p["hit"](t))
    false_hits = [p["id"] for p in measured if not p["same_answer"] and p["hit"](t)]
    misses = sum(1 for p in measured if p["same_answer"] and not p["hit"](t))
    different = sum(1 for p in measured if not p["same_answer"])
    return {"threshold": t, "true_hits": true_hits, "false_hits": len(false_hits),
            "false_hit_ids": false_hits, "misses": misses,
            "false_hit_rate": round(len(false_hits) / different, 4) if different else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=Path, default=ROOT / "eval" / "cache_pairs.yaml")
    ap.add_argument("--out", type=Path, default=Path("cache-threshold.json"))
    args = ap.parse_args()

    pairs = yaml.safe_load(args.pairs.read_text(encoding="utf-8"))["pairs"]
    settings = Settings(retriever="chroma", embed_model="nomic-embed-text",
                        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
                        embed_timeout=120.0,
                        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    embedder = OllamaEmbedder(settings)
    failures: list[str] = []

    # 1. similarities (follow-ups are checked, not measured)
    for p in pairs:
        if p["kind"] == "followup":
            if not is_followup(p["probe"]):
                failures.append(f"{p['id']}: follow-up not detected: {p['probe']!r}")
            p["similarity"] = None
            continue
        p["similarity"] = round(cosine(embedder.embed(p["cached"]), embedder.embed(p["probe"])), 4)

    def hit_fn(p: dict):  # noqa: ANN202
        exact = " ".join(p["cached"].lower().split()).rstrip("?.! ") == \
            " ".join(p["probe"].lower().split()).rstrip("?.! ")
        return lambda t: exact or p["similarity"] >= t

    for p in pairs:
        if p["kind"] != "followup":
            p["hit"] = hit_fn(p)

    # 2. thresholds
    table = [score(pairs, t) for t in THRESHOLDS]
    zero = [row for row in table if row["false_hits"] == 0]
    chosen = zero[0]["threshold"] if zero else None
    if chosen is None:
        failures.append("no threshold in the grid has zero false hits")

    # 3. the real RedisCache: same decisions, never-cache rules, invalidation
    redis_checks: dict = {}
    for t in sorted({x for x in (chosen, 0.95) if x is not None}):
        cache = RedisCache(Settings(redis_url=settings.redis_url, cache_threshold=t,
                                    cache_ttl=600, cache_max_entries=2000))
        cache.r.flushdb()
        gen = f"exp-{t}"
        agree = 0
        for p in pairs:
            if p["kind"] == "followup":
                continue
            cache.r.flushdb()
            cache.store(p["cached"], embedder.embed(p["cached"]), gen, response(p["cached"]))
            got = cache.lookup(p["probe"], embedder.embed(p["probe"]), gen) is not None
            if got == p["hit"](t):
                agree += 1
            else:
                failures.append(f"RedisCache at {t} disagrees on {p['id']}: hit={got}")
        # a new index generation must not see the old entry
        q = pairs[0]["cached"]
        cache.store(q, embedder.embed(q), gen, response(q))
        stale = cache.lookup(q, embedder.embed(q), gen + "-reindexed")
        if stale is not None:
            failures.append(f"RedisCache at {t}: a new generation served a stale answer")
        redis_checks[str(t)] = {"lookups_agreeing": agree,
                                "new_generation_misses": stale is None}

    # never-cache rules, decided before anything reaches Redis
    abstention_cached = should_cache("What is the hostel fee?", response("x", answered=False))
    followups_cached = [p["id"] for p in pairs if p["kind"] == "followup"
                        and should_cache(p["probe"], response(p["probe"]))]
    if abstention_cached:
        failures.append("an abstention would be cached")
    if followups_cached:
        failures.append(f"follow-ups would be cached: {followups_cached}")

    result = {
        "embed_model": settings.embed_model, "pairs": len(pairs),
        "chosen_threshold": chosen, "table": table,
        "similarities": {p["id"]: p["similarity"] for p in pairs},
        "lowest_same_answer_similarity": min(p["similarity"] for p in pairs
                                             if p["similarity"] is not None and p["same_answer"]),
        "highest_different_answer_similarity": max(
            p["similarity"] for p in pairs
            if p["similarity"] is not None and not p["same_answer"]),
        "redis": redis_checks,
        "abstention_cached": abstention_cached, "followups_cached": followups_cached,
        "failures": failures,
    }
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for row in table:
        print(f"t={row['threshold']:.2f}  true_hits={row['true_hits']:>2}  "
              f"false_hits={row['false_hits']:>2} {row['false_hit_ids']}  misses={row['misses']:>2}")
    print("chosen:", chosen, "| failures:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
