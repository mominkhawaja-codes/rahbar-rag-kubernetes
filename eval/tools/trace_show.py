"""Part D2: print the exact context and prompt that produced an answer, from its trace.

    python eval/tools/trace_show.py <trace id> [--dir TRACE_DIR] [--json]

Everything comes from the stored trace record (TRACE_DIR/traces-*.jsonl): the collection is not
queried again, so the output shows what the model saw then, even if the index has changed since.
For an answer served from the cache, the trace that produced the cached answer is shown too.
Exit code 1 when the trace id is not found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def find(trace_dir: str | Path, trace_id: str) -> dict | None:
    for path in sorted(Path(trace_dir).glob("traces-*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if f'"trace_id": "{trace_id}"' in line:
                    record = json.loads(line)
                    if record.get("trace_id") == trace_id:
                        return record
    return None


def context_of(record: dict) -> str | None:
    """The passages exactly as the model was given them (part of the user message)."""
    prompt = record.get("prompt")
    if not prompt:
        return None
    user = next((m["content"] for m in prompt["messages"] if m["role"] == "user"), "")
    match = re.search(r"Passages:\n\n(.*?)\n\nQuestion \(from a student", user, re.S)
    return match.group(1) if match else user


def render(record: dict, trace_dir: str | Path, depth: int = 0) -> str:
    out: list[str] = []
    w = out.append
    w(f"trace {record['trace_id']}  {record['timestamp']}  status={record['status']}  "
      f"outcome={record['outcome']}")
    w(f"query:      {record['query']}")
    col = record.get("collection", {})
    w(f"collection: {col.get('name')}  index_id={col.get('index_id')}  "
      f"built_at={col.get('built_at')}  embed_model={record.get('embedding', {}).get('model')}")
    cache = record.get("cache", {})
    w(f"cache:      {cache.get('result')}" + (f"  (served from trace {cache['served_from_trace_id']}, "
                                              f"similarity {cache.get('similarity')})"
                                              if cache.get("result") == "hit" else ""))
    retrieval = record.get("retrieval", {})
    if retrieval.get("results"):
        w(f"filter:     {json.dumps(retrieval.get('filter'))}")
        w("retrieved:  rank  score   floor  superseded  chunk / document version")
        for r in retrieval["results"]:
            w(f"            {r['rank']:>4}  {r['score']:.4f}  {'yes' if r['above_floor'] else 'no ':<5}"
              f"  {str(r.get('superseded')):<10}  {r['chunk_id']}  ({r.get('doc_id')} {r.get('doc_version')})")
    for g in record.get("guardrails", []):
        detail = {k: v for k, v in g.items() if k not in ("rail", "action")}
        w(f"guardrail:  {g['rail']} -> {g['action']}  {json.dumps(detail, ensure_ascii=False)[:300]}")
    if record.get("error"):
        w(f"error:      {record['error']}")
    response = record.get("response") or {}
    if response:
        w(f"answer:     {response.get('answer')}")

    if cache.get("result") == "hit":
        w("\nNo prompt was built for this request: the answer came from the cache.")
        source = find(trace_dir, cache.get("served_from_trace_id", ""))
        if source and depth < 3:
            w("The request that produced the cached answer:\n")
            w(render(source, trace_dir, depth + 1))
        else:
            w(f"The trace {cache.get('served_from_trace_id')} that produced it is not in {trace_dir}.")
        return "\n".join(out)

    prompt = record.get("prompt")
    if not prompt:
        reason = (record.get("generation", {}).get("reason")
                  or record.get("generation", {}).get("prompt_note")
                  or record.get("generation", {}).get("status"))
        w(f"\nNo prompt was sent to a model for this request ({reason}).")
        return "\n".join(out)
    w(f"\n===== CONTEXT (the passages as the model received them) =====\n{context_of(record)}")
    w(f"\n===== PROMPT (exact messages, sha256 {prompt['sha256']}) =====")
    for message in prompt["messages"]:
        w(f"----- {message['role']} -----\n{message['content']}")
    for attempt in prompt.get("attempts", []):
        w(f"\n----- model call {attempt['attempt']} -----")
        for extra in attempt.get("extra_messages", []):
            w(f"[added for this call] {extra['role']}: {extra['content']}")
        if attempt.get("error"):
            w(f"error: {attempt['error']}")
        w(f"raw output: {attempt.get('raw_output')}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("trace_id")
    ap.add_argument("--dir", default=os.environ.get("TRACE_DIR", "traces"))
    ap.add_argument("--json", action="store_true", help="print the raw trace record")
    args = ap.parse_args()
    record = find(args.dir, args.trace_id)
    if record is None:
        print(f"trace {args.trace_id} not found in {args.dir}", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(record, indent=2, ensure_ascii=False) if args.json else render(record, args.dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
