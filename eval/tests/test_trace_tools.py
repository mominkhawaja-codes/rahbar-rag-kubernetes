"""Part D2 and D3 tools on hand-made trace records (no API, no model)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import trace_show  # noqa: E402
import trace_summary  # noqa: E402

SYSTEM = "You are RAHBAR ... rules ..."
USER = ('Passages:\n\n<passage n="1" source="faq.md" section="Cards [1.2]" page="1">\n'
        'A card costs Rs. 500.\n</passage>\n\nQuestion (from a student; answer it only from '
        'the passages above):\nHow much is a card?')


def record(tid: str, ts: str, *, outcome: str = "answered", cache: str = "miss",
           top1: float | None = 0.8, prompt: bool = True, served_from: str | None = None,
           rails: list[dict] | None = None, ms: float = 100.0, reason: str | None = None) -> dict:
    r = {"trace_version": 1, "trace_id": tid, "timestamp": ts, "query": "How much is a card?",
         "status": "ok" if outcome != "error" else "failed", "outcome": outcome,
         "collection": {"name": "rahbar", "index_id": "abc"}, "embedding": {"model": "m"},
         "cache": {"result": cache}, "guardrails": rails or [],
         "retrieval": {"status": "ran", "filter": {"superseded": False}, "results": [
             {"rank": 1, "chunk_id": "c1", "doc_id": "faq.md", "doc_version": "2026-09",
              "superseded": False, "score": top1, "above_floor": True}]} if top1 is not None
         else {"status": "skipped"},
         "prompt": {"messages": [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": USER}],
                    "sha256": "x", "attempts": [{"attempt": 1, "extra_messages": [],
                                                 "raw_output": "{...}"}]} if prompt else None,
         "generation": {"status": "ran" if prompt else "skipped"},
         "response": {"answer": "A card costs Rs. 500. [1]"},
         "stages": {"retrieval": {"status": "ran", "ms": ms},
                    "generation": {"status": "ran" if prompt else "skipped",
                                   "ms": ms * 10 if prompt else None}},
         "latency_ms": {"total": ms * 11}}
    if served_from:
        r["cache"] = {"result": "hit", "served_from_trace_id": served_from, "similarity": 1.0}
        r["stages"]["retrieval"] = {"status": "skipped", "ms": None}
    if reason:
        r["abstention"] = {"reason": reason}
    return r


def write(tmp: Path, *records: dict) -> Path:
    (tmp / "traces-2026-11-28.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return tmp


def test_trace_show_prints_the_exact_context_and_prompt(tmp_path: Path, capsys) -> None:
    write(tmp_path, record("t1", "2026-11-28T10:00:00.000Z"))
    out = trace_show.render(trace_show.find(tmp_path, "t1"), tmp_path)
    assert '<passage n="1" source="faq.md" section="Cards [1.2]" page="1">' in out
    assert SYSTEM in out and USER in out and "raw output: {...}" in out
    assert trace_show.context_of(trace_show.find(tmp_path, "t1")).startswith("<passage n=\"1\"")


def test_trace_show_follows_a_cache_hit_to_the_trace_that_made_the_answer(tmp_path: Path) -> None:
    write(tmp_path, record("t1", "2026-11-28T10:00:00.000Z"),
          record("t2", "2026-11-28T10:01:00.000Z", prompt=False, top1=None, served_from="t1"))
    out = trace_show.render(trace_show.find(tmp_path, "t2"), tmp_path)
    assert "came from the cache" in out and "served from trace t1" in out and USER in out


def test_trace_show_says_when_no_prompt_was_sent(tmp_path: Path) -> None:
    write(tmp_path, record("t3", "2026-11-28T10:00:00.000Z", prompt=False, outcome="abstained"))
    assert "No prompt was sent" in trace_show.render(trace_show.find(tmp_path, "t3"), tmp_path)
    assert trace_show.find(tmp_path, "missing") is None


def test_trace_summary_metrics_and_time_window(tmp_path: Path) -> None:
    inj = [{"rail": "passage_injection_scan", "action": "flagged", "sent_to_model": True}]
    write(tmp_path,
          record("a", "2026-11-28T09:00:00.000Z", top1=0.95),                       # outside
          record("b", "2026-11-28T10:00:00.000Z", top1=0.8, ms=100, rails=inj),
          record("c", "2026-11-28T10:10:00.000Z", top1=0.6, ms=200, outcome="abstained",
                 prompt=False, reason="model"),
          record("d", "2026-11-28T10:20:00.000Z", top1=None, prompt=False, served_from="b"),
          record("e", "2026-11-28T10:30:00.000Z", top1=0.4, ms=300, outcome="citation_failed",
                 reason="no_valid_citation"))
    since = datetime(2026, 11, 28, 9, 30, tzinfo=UTC)
    s = trace_summary.summarise(trace_summary.load(tmp_path, since, None))
    assert s["traces"] == 4
    assert s["top1_similarity"]["n"] == 3 and s["top1_similarity"]["max"] == 0.8
    assert s["top1_similarity"]["histogram"]["0.6-0.7"] == 1
    assert s["abstention_rate"] == round(2 / 4, 4)
    assert s["abstentions"]["reasons"] == {"model": 1, "no_valid_citation": 1}
    assert s["cache_hit_rate"] == 0.25 and s["cache"]["hit"] == 1
    assert s["latency_ms"]["retrieval"]["n"] == 3
    assert s["latency_ms"]["retrieval"]["p95"] == trace_summary.percentile([100, 200, 300], .95)
    assert s["injection_attempts_flagged"]["in_retrieved_passages"] == 1
    assert "top-1 similarity" in trace_summary.table(s)


def test_percentile_and_window_parsing() -> None:
    assert trace_summary.percentile([], .95) is None
    assert trace_summary.percentile([1, 2, 3, 4], .5) == 2.5
    assert trace_summary.parse_window("30m").total_seconds() == 1800
