#!/usr/bin/env python3
"""Score a running RAHBAR against a golden set, and gate CI on the result (section 5).

    python eval/run_eval.py --api http://<ingress>                 # your corpus
    python eval/run_eval.py --api ... --golden eval/fixtures/golden.fixture.yaml
    python eval/run_eval.py --api ... --update-baseline            # after a deliberate improvement

Exit code 0 when every gate passes, 1 when any fails. CI relies on that exit code.

Talking to the API, reading the golden set and writing results.json came with the kit;
recall_at_k, citation_validity, summarise and gate are ours (eval/tests/test_run_eval.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent

# The gate from section 5.4. Change these only with a reason written in docs/PIPELINE.md.
MAX_RECALL5_DROP = 0.05        # recall@5 may fall at most 5 points below the baseline
MIN_CITATION_VALIDITY = 0.95   # at least 95% of citations must match retrieved passages
REQUIRED_ABSTENTION = 1.0      # every must-abstain question must be declined


def load_golden(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    questions = data.get("questions") or []
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def ask(client: httpx.Client, api: str, question: str) -> dict:
    """POST one question and return the AskResponse as a dict (see api/app/schemas.py)."""
    response = client.post(f"{api.rstrip('/')}/api/ask", json={"question": question})
    response.raise_for_status()
    return response.json()


_CLAUSE_NO = re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,2}$")
_RANGE = re.compile(r"\[(\d{1,2}(?:\.\d{1,2}){1,2})(?:-(\d{1,2}(?:\.\d{1,2}){1,2}))?\]")


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _clause_tuple(clause: str) -> tuple[int, ...]:
    return tuple(int(x) for x in clause.split("."))


def section_matches(golden: str, retrieved: str) -> bool:
    """How a golden section matches a retrieved one (docs/PIPELINE.md, "Evaluation"):

    * a clause number ("5.4") matches a section whose clause range contains it:
      "Academic Warning/Probation [5.4-5.7]";
    * anything else matches when the retrieved section equals it or starts with it:
      "1. Attendance" matches "1. Attendance [1.1-1.2]".
    """
    g, r = _norm(golden), _norm(retrieved)
    if not g:
        return False
    if _CLAUSE_NO.match(g):
        m = _RANGE.search(r)
        if m:
            lo = _clause_tuple(m.group(1))
            hi = _clause_tuple(m.group(2) or m.group(1))
            return lo <= _clause_tuple(g) <= hi
        return r.startswith(g + " ") or r == g
    return r == g or r.startswith(g)


def _found(rel: dict, retrieved: list[dict]) -> bool:
    return any(_norm(p.get("source", "")) == _norm(rel.get("source", ""))
               and section_matches(rel.get("section", ""), p.get("section", ""))
               for p in retrieved)


def recall_at_k(relevant: list[dict], retrieved: list[dict], k: int) -> float | None:
    """Fraction of the relevant (source, section) pairs found in the top k retrieved.
    Return None when the question has no relevant passages (must-abstain questions)."""
    if not relevant:
        return None
    top = retrieved[:k]
    return sum(1 for rel in relevant if _found(rel, top)) / len(relevant)


def citation_validity(response: dict) -> float | None:
    """Fraction of the response's citations that match a passage in response['retrieved'].
    Return None when there are no citations."""
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = {(_norm(p.get("source", "")), _norm(p.get("section", "")))
                 for p in response.get("retrieved") or []}
    valid = sum(1 for c in citations
                if (_norm(c.get("source", "")), _norm(c.get("section", ""))) in retrieved)
    return valid / len(citations)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _contains(answer: str, expect: str) -> bool:
    squash = lambda s: re.sub(r"[\s,]", "", s.lower())  # noqa: E731 - "12,000" == "12000"
    return squash(expect) in squash(answer)


def summarise(rows: list[dict]) -> dict:
    """Overall recall@3, recall@5, citation validity and abstention correctness, plus
    recall@5 broken down by question type. This dict becomes results.json."""
    per_question = []
    r3, r5, by_type = [], [], {}
    cited = valid_cited = 0
    abstain_ok = abstain_total = 0
    false_abstain = answerable = 0
    expect_hits = expect_total = 0
    for row in rows:
        resp = row["response"]
        retrieved = resp.get("retrieved") or []
        rec3 = recall_at_k(row["relevant"], retrieved, 3)
        rec5 = recall_at_k(row["relevant"], retrieved, 5)
        if rec3 is not None:
            r3.append(rec3)
        if rec5 is not None:
            r5.append(rec5)
            by_type.setdefault(row["type"], []).append(rec5)
        validity = citation_validity(resp)
        n_cit = len(resp.get("citations") or [])
        if validity is not None:
            cited += n_cit
            valid_cited += round(validity * n_cit)
        answered = bool(resp.get("answered_from_context"))
        if row["must_abstain"]:
            abstain_total += 1
            abstain_ok += int(not answered)
        else:
            answerable += 1
            false_abstain += int(not answered)
        hit = None
        if row.get("expect") and not row["must_abstain"]:
            expect_total += 1
            hit = answered and _contains(resp.get("answer", ""), str(row["expect"]))
            expect_hits += int(bool(hit))
        per_question.append({
            "id": row["id"], "type": row["type"], "recall@3": rec3, "recall@5": rec5,
            "citation_validity": validity, "answered_from_context": answered,
            "must_abstain": row["must_abstain"], "expect_found": hit,
            "cached": bool(resp.get("cached")), "seconds": row.get("seconds"),
            "top_score": retrieved[0]["score"] if retrieved else None,
        })
    return {
        "recall@3": _mean(r3),
        "recall@5": _mean(r5),
        "recall@5_by_type": {t: _mean(v) for t, v in sorted(by_type.items())},
        "citation_validity": round(valid_cited / cited, 4) if cited else None,
        "citations": cited,
        "abstention_correctness": round(abstain_ok / abstain_total, 4) if abstain_total else None,
        "false_abstention_rate": round(false_abstain / answerable, 4) if answerable else None,
        "expect_found_rate": round(expect_hits / expect_total, 4) if expect_total else None,
        "per_question": per_question,
    }


def gate(results: dict, baseline: dict | None) -> list[str]:
    """Return one message per failed gate (section 5.4); an empty list means pass."""
    failures = []
    recall = results.get("recall@5")
    if baseline and baseline.get("recall@5") is not None and recall is not None:
        floor = baseline["recall@5"] - MAX_RECALL5_DROP
        if recall < floor - 1e-9:
            failures.append(f"recall@5 {recall:.3f} is more than {MAX_RECALL5_DROP:.2f} below "
                            f"the baseline {baseline['recall@5']:.3f}")
    validity = results.get("citation_validity")
    if validity is not None and validity < MIN_CITATION_VALIDITY:
        failures.append(f"citation validity {validity:.3f} < {MIN_CITATION_VALIDITY:.2f}")
    abstention = results.get("abstention_correctness")
    if abstention is not None and abstention < REQUIRED_ABSTENTION:
        missed = [q["id"] for q in results.get("per_question", [])
                  if q["must_abstain"] and q["answered_from_context"]]
        failures.append(f"abstention correctness {abstention:.2f} < {REQUIRED_ABSTENTION:.2f}; "
                        f"answered instead of declining: {', '.join(missed)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate RAHBAR against a golden set.")
    parser.add_argument("--api", required=True, help="base URL, e.g. http://192.168.49.2")
    parser.add_argument("--host", help="Host header, when your Ingress rule names a host")
    parser.add_argument("--golden", type=Path, default=HERE / "golden.yaml")
    parser.add_argument("--baseline", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds per question; raise it for a slow CPU-only model")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write these results as the new baseline")
    args = parser.parse_args()

    questions = load_golden(args.golden)
    rows = []
    headers = {"Host": args.host} if args.host else {}
    with httpx.Client(timeout=args.timeout, headers=headers) as client:
        for q in questions:
            started = time.perf_counter()
            response = ask(client, args.api, q["question"])
            rows.append({
                "id": q["id"], "type": q.get("type", "ordinary"),
                "must_abstain": bool(q.get("must_abstain", False)),
                "relevant": q.get("relevant") or [],
                "expect": q.get("expect"),
                "response": response,
                "seconds": round(time.perf_counter() - started, 2),
            })
            print(f"  {q['id']:<5} {rows[-1]['seconds']:>6.2f}s  {q['question'][:60]}")

    try:
        results = summarise(rows)
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else None
        failures = gate(results, baseline)
    except NotImplementedError as exc:
        print(f"\nnot implemented yet: {exc}")
        return 2

    results["golden"] = str(args.golden)
    results["questions"] = len(rows)
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nresults written to {args.out}")
    if args.update_baseline:
        args.baseline.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"baseline updated: {args.baseline}")

    if failures:
        print("\nGATE FAILED")
        for message in failures:
            print(f"  - {message}")
        return 1
    print("\ngate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
