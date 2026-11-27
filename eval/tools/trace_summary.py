"""Part D3: summarise the trace log over a time window.

    python eval/tools/trace_summary.py [--dir TRACE_DIR] [--last 1h | --since T --until T]
                                       [--out summary.json]

Reports, for the traces in the window: the top-1 retrieval similarity distribution, the
abstention rate, the cache hit rate, p50 and p95 latency per stage, and the number of injection
attempts flagged (in questions and in retrieved passages). Times are UTC, ISO 8601
(2026-11-28T10:00:00Z); --last takes 30m, 2h or 1d and counts back from now.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ANSWERED = "answered"
ABSTAINED = ("abstained", "citation_failed")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def parse_window(last: str) -> timedelta:
    unit = last[-1]
    amount = float(last[:-1])
    return {"m": timedelta(minutes=amount), "h": timedelta(hours=amount),
            "d": timedelta(days=amount)}[unit]


def load(trace_dir: str | Path, since: datetime | None, until: datetime | None) -> list[dict]:
    records = []
    for path in sorted(Path(trace_dir).glob("traces-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            ts = parse_time(record["timestamp"])
            if (since is None or ts >= since) and (until is None or ts < until):
                records.append(record)
    return records


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 4)


def summarise(records: list[dict]) -> dict:
    top1 = [r["retrieval"]["results"][0]["score"] for r in records
            if r.get("retrieval", {}).get("results")]
    bins = {f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}": 0 for lo in range(10)}
    for score in top1:
        lo = min(9, max(0, int(score * 10)))
        bins[f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}"] += 1
    outcomes: dict[str, int] = {}
    for r in records:
        outcomes[str(r.get("outcome"))] = outcomes.get(str(r.get("outcome")), 0) + 1
    decided = sum(outcomes.get(k, 0) for k in (ANSWERED, *ABSTAINED, "generation_failed"))
    abstained = sum(outcomes.get(k, 0) for k in ABSTAINED)
    reasons: dict[str, int] = {}
    for r in records:
        if r.get("outcome") in ABSTAINED:
            reason = (r.get("abstention") or {}).get("reason") or "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1
    cache = {"hit": 0, "miss": 0, "skip": 0, "error": 0}
    for r in records:
        result = (r.get("cache") or {}).get("result")
        if result in cache:
            cache[result] += 1
    lookups = cache["hit"] + cache["miss"]
    stages: dict[str, list[float]] = {}
    for r in records:
        for name, stage in (r.get("stages") or {}).items():
            if stage.get("status") == "ran" and stage.get("ms") is not None:
                stages.setdefault(name, []).append(stage["ms"])
        if (r.get("latency_ms") or {}).get("total") is not None:
            stages.setdefault("total", []).append(r["latency_ms"]["total"])
    rails = [g for r in records for g in r.get("guardrails", [])]
    q_flagged = [r["trace_id"] for r in records
                 if any(g["rail"] == "input_injection_scan" and g["action"] == "flagged"
                        for g in r.get("guardrails", []))]
    p_flagged = [r["trace_id"] for r in records
                 if any(g["rail"] == "passage_injection_scan" and g["action"] == "flagged"
                        for g in r.get("guardrails", []))]
    return {
        "traces": len(records),
        "window": {"first": records[0]["timestamp"] if records else None,
                   "last": records[-1]["timestamp"] if records else None},
        "status": {s: sum(1 for r in records if r.get("status") == s) for s in ("ok", "failed")},
        "outcomes": outcomes,
        "top1_similarity": {
            "n": len(top1), "min": percentile(top1, 0), "p25": percentile(top1, .25),
            "p50": percentile(top1, .5), "p75": percentile(top1, .75),
            "p95": percentile(top1, .95), "max": percentile(top1, 1),
            "mean": round(sum(top1) / len(top1), 4) if top1 else None, "histogram": bins},
        "abstention_rate": round(abstained / decided, 4) if decided else None,
        "abstentions": {"count": abstained, "of": decided, "reasons": reasons},
        "cache_hit_rate": round(cache["hit"] / lookups, 4) if lookups else None,
        "cache": cache,
        "latency_ms": {name: {"n": len(v), "p50": percentile(v, .5), "p95": percentile(v, .95)}
                       for name, v in sorted(stages.items())},
        "injection_attempts_flagged": {
            "traces_with_any": len(set(q_flagged) | set(p_flagged)),
            "in_question": len(q_flagged), "in_retrieved_passages": len(p_flagged),
            "passages_sent_to_model": sum(1 for g in rails if g["rail"] == "passage_injection_scan"
                                          and g.get("sent_to_model")),
            "answer_echoed_injection": sum(1 for g in rails
                                           if g["rail"] == "output_injection_echo_check"
                                           and g["action"] == "echo_detected")},
    }


def table(s: dict) -> str:
    lines = [f"traces: {s['traces']}  ({s['window']['first']} .. {s['window']['last']})",
             f"status: {s['status']}   outcomes: {s['outcomes']}"]
    t = s["top1_similarity"]
    lines.append(f"top-1 similarity: n={t['n']} min={t['min']} p25={t['p25']} p50={t['p50']} "
                 f"p75={t['p75']} max={t['max']} mean={t['mean']}")
    lines.append("  histogram: " + "  ".join(f"{k}:{v}" for k, v in t["histogram"].items() if v))
    lines.append(f"abstention rate: {s['abstention_rate']}  ({s['abstentions']})")
    lines.append(f"cache hit rate: {s['cache_hit_rate']}  ({s['cache']})")
    lines.append("latency per stage (ms):")
    for name, v in s["latency_ms"].items():
        lines.append(f"  {name:<16} n={v['n']:<4} p50={v['p50']:<10} p95={v['p95']}")
    lines.append(f"injection attempts flagged: {s['injection_attempts_flagged']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.environ.get("TRACE_DIR", "traces"))
    ap.add_argument("--last", help="window ending now: 30m, 2h, 1d")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    until = parse_time(args.until) if args.until else None
    since = parse_time(args.since) if args.since else None
    if args.last:
        until = until or datetime.now(UTC)
        since = until - parse_window(args.last)
    summary = summarise(load(args.dir, since, until))
    summary["window"]["requested"] = {"since": since.isoformat() if since else None,
                                      "until": until.isoformat() if until else None}
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(table(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
