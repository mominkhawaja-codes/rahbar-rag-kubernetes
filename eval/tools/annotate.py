"""Print a results JSON file as one GitHub Actions notice annotation.

Annotations can be read from the public check-runs API without repository access, so a
run's numbers can be collected and committed by anyone who can see the run. GitHub cuts
an annotation at 4096 characters, so the per-question rows are written in a compact
positional form; `expand` turns them back into results.json rows.

    python eval/tools/annotate.py results-final.json
"""

from __future__ import annotations

import json
import sys

ROW = ["id", "type", "recall@3", "recall@5", "citation_validity", "answered_from_context",
       "must_abstain", "expect_found", "top_score", "seconds", "error"]


def compact(results: dict) -> dict:
    out = {k: v for k, v in results.items() if k != "per_question"}
    out["rows"] = [[q.get(k) if k != "error" else (q.get(k) or "")[:60] for k in ROW]
                   for q in results.get("per_question", [])]
    out["row_fields"] = ROW
    return out


def expand(data: dict) -> dict:
    fields = data.pop("row_fields", ROW)
    data["per_question"] = [dict(zip(fields, row)) for row in data.pop("rows", [])]
    return data


if __name__ == "__main__":
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as fh:
            body = json.dumps(compact(json.load(fh)), separators=(",", ":"))
        if len(body) > 4000:
            print(f"::warning title={path}::annotation would be {len(body)} chars; truncated")
        body = body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::notice title={path}::{body}")
