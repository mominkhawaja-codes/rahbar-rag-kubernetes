#!/usr/bin/env python3
"""Measure the semantic-cache threshold with the real embedding model (ADR 0003).

Needs a running Ollama with nomic-embed-text and a running Redis:

    OLLAMA_URL=http://localhost:11434 REDIS_URL=redis://localhost:6379/0 \
        python eval/cache_threshold.py --out cache-threshold.json

1. Embeds original and recognised canonical questions with the API's OllamaEmbedder.
2. Compares the cosine-only and guarded policies over the same labelled pairs.
3. Replays the guarded lookups in distinct expiring Redis namespaces and
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
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.config import Settings  # noqa: E402
from app.embeddings import OllamaEmbedder, cosine  # noqa: E402
from app.schemas import AskResponse, Citation, RetrievedRef  # noqa: E402
from app.services.cache import (RedisCache, is_followup, normalise_question,  # noqa: E402
                                semantic_cache_intent, should_cache)

THRESHOLDS = [0.80, 0.85, 0.88, 0.90, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]


def response(question: str, answered: bool = True) -> AskResponse:
    ref = RetrievedRef(chunk_id="c1", source="doc.pdf", section="S [1.1]", page=1, score=0.8)
    return AskResponse(request_id="exp", answer=f"answer to: {question}",
                       citations=[Citation(source="doc.pdf", section="S [1.1]", page=1)]
                       if answered else [],
                       answered_from_context=answered, confidence="high" if answered else "low",
                       retrieved=[ref], provider="experiment")


def predicts_hit(pair: dict, threshold: float, guarded: bool) -> bool:
    similarity = pair["guarded_similarity" if guarded else "similarity"]
    return pair["exact"] or (similarity is not None and similarity >= threshold)


def score(pairs: list[dict], t: float, guarded: bool) -> dict:
    measured = [p for p in pairs if p["kind"] != "followup"]
    true_hits = sum(1 for p in measured if p["same_answer"] and predicts_hit(p, t, guarded))
    false_hits = [p["id"] for p in measured if not p["same_answer"] and predicts_hit(p, t, guarded)]
    misses = sum(1 for p in measured if p["same_answer"] and not predicts_hit(p, t, guarded))
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
                        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                        ollama_num_threads=int(os.environ.get("OLLAMA_NUM_THREADS", "0")))
    embedder = OllamaEmbedder(settings)
    failures: list[str] = []

    # 1. similarities (follow-ups are checked, not measured)
    for p in pairs:
        if p["kind"] == "followup":
            if not is_followup(p["probe"]):
                failures.append(f"{p['id']}: follow-up not detected: {p['probe']!r}")
            p["similarity"] = None
            continue
        p["similarity"] = cosine(embedder.embed(p["cached"]), embedder.embed(p["probe"]))
        p["exact"] = normalise_question(p["cached"]) == normalise_question(p["probe"])
        a, b = semantic_cache_intent(p["cached"]), semantic_cache_intent(p["probe"])
        p["guarded_similarity"] = cosine(embedder.embed(a), embedder.embed(b)) if a and a == b else None

    # 2. thresholds
    before = [score(pairs, t, guarded=False) for t in THRESHOLDS]
    table = [score(pairs, t, guarded=True) for t in THRESHOLDS]
    chosen = settings.cache_threshold
    if score(pairs, chosen, guarded=True)["false_hits"]:
        failures.append("the configured threshold permits a false hit")

    # 3. the real RedisCache: same decisions, never-cache rules, invalidation
    cache = RedisCache(Settings(redis_url=settings.redis_url, cache_threshold=chosen,
                                cache_ttl=600, cache_max_entries=2000))
    run = uuid4().hex
    agree = 0
    for p in pairs:
        if p["kind"] == "followup":
            continue
        gen = f"exp-{run}-{p['id']}"
        cached_text = semantic_cache_intent(p["cached"]) or p["cached"]
        probe_text = semantic_cache_intent(p["probe"]) or p["probe"]
        cache.store(p["cached"], embedder.embed(cached_text), gen, response(p["cached"]))
        got = cache.lookup(p["probe"], embedder.embed(probe_text), gen) is not None
        if got == predicts_hit(p, chosen, guarded=True):
            agree += 1
        else:
            failures.append(f"RedisCache at {chosen} disagrees on {p['id']}: hit={got}")
    q = pairs[0]["cached"]
    gen = f"exp-{run}-invalidation"
    cache.store(q, embedder.embed(q), gen, response(q))
    stale = cache.lookup(q, embedder.embed(q), gen + "-reindexed")
    if stale is not None:
        failures.append(f"RedisCache at {chosen}: a new generation served a stale answer")
    redis_checks = {str(chosen): {"lookups_agreeing": agree,
                                 "new_generation_misses": stale is None}}

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
        "chosen_threshold": chosen, "table": table, "cosine_only_table": before,
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
