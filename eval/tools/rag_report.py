#!/usr/bin/env python3
"""Summarise one real-model RAG evaluation run (.github/workflows/rag-eval.yml).

    python eval/tools/rag_report.py --results results-real.json \
        --metrics-before m0.txt --metrics-after m1.txt --api-log api.log \
        --ollama-state ollama.json --duration 1234 --out report.json

Counts are per golden question:
  correct            answerable, answered from context, expected fact in the answer
  incorrect          answerable, answered from context, expected fact missing
  false_abstention   answerable, declined, the request itself succeeded
  correct_abstention must-abstain, declined, the request itself succeeded
  wrongly_answered   must-abstain, answered from context
  failed             the HTTP request failed (status or timeout), split by kind
The API log lines are joined to questions by request id (run_eval sends the question id).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

SAMPLE = re.compile(r"^(\w+)(?:\{(.*)\})?\s+([0-9.eE+-]+)$")


def parse_metrics(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = SAMPLE.match(line.strip())
        if not m:
            continue
        labels = tuple(sorted(re.findall(r'(\w+)="([^"]*)"', m.group(2) or "")))
        out[(m.group(1), labels)] = float(m.group(3))
    return out


def delta(before: dict, after: dict, name: str, by: str | None = None) -> dict | float:
    keys = {k for k in after if k[0] == name}
    if by is None:
        return round(sum(after[k] - before.get(k, 0.0) for k in keys), 3)
    out: dict[str, float] = {}
    for k in keys:
        label = dict(k[1]).get(by, "?")
        out[label] = out.get(label, 0.0) + after[k] - before.get(k, 0.0)
    return {k: round(v, 3) for k, v in sorted(out.items()) if v}


def error_kind(error: str) -> str:
    if "Timeout" in error:
        return "timeout"
    m = re.search(r"'(\d{3}) ", error) or re.search(r"\b(\d{3})\b", error)
    return f"http_{m.group(1)}" if m else error.split(":")[0]


def parse_log(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def classify(q: dict) -> str:
    if q.get("error"):
        return "failed"
    if q["must_abstain"]:
        return "wrongly_answered" if q["answered_from_context"] else "correct_abstention"
    if not q["answered_from_context"]:
        return "false_abstention"
    return "correct" if q.get("expect_found") else "incorrect"


def build(results: dict, m0: str, m1: str, log: str, ollama: dict | None,
          duration: float | None) -> dict:
    before, after = parse_metrics(m0), parse_metrics(m1)
    lines = parse_log(log)
    declined = {r.get("request_id"): r.get("declined") for r in lines if r.get("msg") == "answered"}
    cached = {r.get("request_id") for r in lines if r.get("msg") == "answered" and r.get("cache") == "hit"}
    rows = []
    for q in results.get("per_question", []):
        rows.append({"id": q["id"], "type": q["type"], "outcome": classify(q),
                     "declined": declined.get(q["id"]), "cached": q["id"] in cached,
                     "recall@5": q.get("recall@5"), "top": q.get("top_score"),
                     "s": q.get("seconds"), "error": error_kind(q["error"]) if q.get("error") else None})
    outcomes = Counter(r["outcome"] for r in rows)
    deps = Counter(f"{r.get('dependency')}: {str(r.get('error'))[:90]}"
                   for r in lines if r.get("msg") == "dependency unavailable")
    gen_fail = Counter(str(r.get("reason")) for r in lines
                       if r.get("msg") == "generation failed; using fallback")
    return {
        "questions": len(rows),
        "answerable": sum(1 for r in rows if r["type"] != "abstain"),
        **{k: outcomes.get(k, 0) for k in ("correct", "incorrect", "false_abstention",
                                           "correct_abstention", "wrongly_answered", "failed")},
        "failures_by_kind": dict(Counter(r["error"] for r in rows if r["error"])),
        "recall@3": results.get("recall@3"), "recall@5": results.get("recall@5"),
        "recall@5_by_type": results.get("recall@5_by_type"),
        "citation_validity": results.get("citation_validity"), "citations": results.get("citations"),
        "fallbacks": delta(before, after, "rahbar_fallbacks_total", "reason"),
        "abstentions_by_reason": delta(before, after, "rahbar_abstentions_total", "reason"),
        "citations_dropped": delta(before, after, "rahbar_citations_dropped_total"),
        "cache": delta(before, after, "rahbar_cache_total", "result"),
        "llm_tokens": delta(before, after, "rahbar_llm_tokens_total", "direction"),
        "dependency_errors": dict(deps.most_common(6)),
        "generation_failures": dict(gen_fail),
        "ollama": ollama, "duration_s": duration, "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--metrics-before", type=Path, required=True)
    ap.add_argument("--metrics-after", type=Path, required=True)
    ap.add_argument("--api-log", type=Path, required=True)
    ap.add_argument("--ollama-state", type=Path)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    read = lambda p: p.read_text(encoding="utf-8", errors="replace") if p and p.exists() else ""  # noqa: E731
    ollama = json.loads(read(a.ollama_state)) if a.ollama_state and a.ollama_state.exists() else None
    report = build(json.loads(read(a.results)), read(a.metrics_before), read(a.metrics_after),
                   read(a.api_log), ollama, a.duration)
    a.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
