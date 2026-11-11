"""The real-model evaluation report: outcome classes, metric deltas, log joins."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import rag_report as rr  # noqa: E402


def q(qid: str, qtype: str, answered: bool, expect_found: bool | None = None,
      error: str | None = None, abstain: bool = False) -> dict:
    return {"id": qid, "type": qtype, "must_abstain": abstain, "answered_from_context": answered,
            "expect_found": expect_found, "error": error, "recall@5": 1.0, "top_score": 0.8,
            "seconds": 9.0}


def test_outcomes_keep_failures_apart_from_abstentions() -> None:
    assert rr.classify(q("a", "ordinary", True, True)) == "correct"
    assert rr.classify(q("b", "ordinary", True, False)) == "incorrect"
    assert rr.classify(q("c", "ordinary", False)) == "false_abstention"
    assert rr.classify(q("d", "abstain", False, abstain=True)) == "correct_abstention"
    assert rr.classify(q("e", "abstain", True, abstain=True)) == "wrongly_answered"
    assert rr.classify(q("f", "abstain", False, error="HTTPStatusError: 503", abstain=True)) == "failed"


def test_error_kinds() -> None:
    assert rr.error_kind("HTTPStatusError: Server error '503 Service Unavailable' for url") == "http_503"
    assert rr.error_kind("ReadTimeout: timed out") == "timeout"


METRICS_0 = """# HELP rahbar_fallbacks_total x
rahbar_fallbacks_total{reason="ReadTimeout"} 1.0
rahbar_abstentions_total{reason="model"} 2.0
rahbar_cache_total{result="miss"} 3.0
"""
METRICS_1 = """rahbar_fallbacks_total{reason="ReadTimeout"} 2.0
rahbar_fallbacks_total{reason="GenerationFailed"} 1.0
rahbar_abstentions_total{reason="model"} 5.0
rahbar_cache_total{result="miss"} 5.0
rahbar_citations_dropped_total 4.0
"""


def test_metric_deltas_by_label() -> None:
    m0, m1 = rr.parse_metrics(METRICS_0), rr.parse_metrics(METRICS_1)
    assert rr.delta(m0, m1, "rahbar_fallbacks_total", "reason") == {
        "GenerationFailed": 1.0, "ReadTimeout": 1.0}
    assert rr.delta(m0, m1, "rahbar_abstentions_total", "reason") == {"model": 3.0}
    assert rr.delta(m0, m1, "rahbar_citations_dropped_total") == 4.0


def test_report_joins_log_lines_by_request_id() -> None:
    results = {"recall@5": 0.9, "per_question": [
        q("q01", "ordinary", False), q("q17", "abstain", False, error="HTTPStatusError: '503 x'",
                                       abstain=True)]}
    log = "\n".join(json.dumps(x) for x in [
        {"msg": "answered", "request_id": "q01", "declined": "model", "cache": "miss"},
        {"msg": "dependency unavailable", "request_id": "q17", "dependency": "embedding_model",
         "error": "embedding model nomic-embed-text unavailable: timed out"},
    ])
    r = rr.build(results, METRICS_0, METRICS_1, log, {"restarts": 2}, 100.0)
    assert r["false_abstention"] == 1 and r["failed"] == 1
    assert r["rows"][0]["declined"] == "model"
    assert r["failures_by_kind"] == {"http_503": 1}
    assert list(r["dependency_errors"]) == [
        "embedding_model: embedding model nomic-embed-text unavailable: timed out"]
    assert r["ollama"] == {"restarts": 2}
