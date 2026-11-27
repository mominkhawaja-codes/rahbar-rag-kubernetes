"""Part D1: one JSON-line trace per query (Assignment 03; paper 3.2.3, 5.1, Table 2).

Every call to `answer_question` produces exactly one record, whether the question was
answered, declined, served from the cache or failed. The record is written to stdout (so
`kubectl logs` shows it) and, when TRACE_DIR is set, appended to
`TRACE_DIR/traces-<UTC date>[-<pod>].jsonl` for the report scripts (eval/tools/trace_*.py).

What a record holds: the trace id (the request id), the question, the embedding model and the
collection with its `index_id`, every retrieved chunk with its document id, version, score and
whether it cleared the relevance floor, the metadata filter, the order the passages were given
to the model, the exact messages sent to the model and its raw replies, the model and its
parameters, the response and citations, the guardrail actions, and the latency and status of
every stage. A stage that did not run says so (`skipped`, with the reason): nothing is filled
in for work that did not happen. A cache hit records the trace id of the request that produced
the cached answer instead of a retrieval or a prompt it never made.

Never recorded: the API key, request headers, or any setting that is not listed below.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

TRACE_VERSION = 1
trace_var: ContextVar[Trace | None] = ContextVar("trace", default=None)
_file_lock = threading.Lock()


def current() -> Trace | None:
    return trace_var.get()


def prompt_sha256(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


class Trace:
    def __init__(self, trace_id: str, question: str) -> None:
        self._t0 = time.perf_counter()
        now = time.time()
        self.record: dict[str, Any] = {
            "trace_version": TRACE_VERSION,
            "logger": "rahbar.trace",
            "trace_id": trace_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
                         + f".{int(now % 1 * 1000):03d}Z",
            "query": question,
            "rewritten_query": None,
            "query_rewrite": "none: retrieval and generation use the question as asked",
            "status": "running",
            "outcome": None,
            "pipeline": {},
            "embedding": {},
            "collection": {},
            "cache": {},
            "retrieval": {"status": "not reached"},
            "rerank": {"applied": False, "note": "no reranker: passages keep retrieval order"},
            "prompt": None,
            "generation": {"status": "not reached"},
            "response": None,
            "citations": [],
            "guardrails": [],
            "stages": {},
            "latency_ms": {},
            "error": None,
        }

    # ---------------------------------------------------------------- stages
    @contextmanager
    def stage(self, name: str, note: str = ""):
        started = time.perf_counter()
        entry: dict[str, Any] = {"status": "ran"}
        if note:
            entry["note"] = note
        self.record["stages"][name] = entry
        try:
            yield entry
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            entry["ms"] = round((time.perf_counter() - started) * 1000, 2)

    def skip(self, name: str, reason: str) -> None:
        self.record["stages"][name] = {"status": "skipped", "reason": reason, "ms": None}

    def guardrail(self, rail: str, action: str, **detail: Any) -> None:
        self.record["guardrails"].append({"rail": rail, "action": action, **detail})

    def set(self, key: str, value: Any) -> None:
        self.record[key] = value

    def update(self, key: str, **values: Any) -> None:
        self.record.setdefault(key, {}).update(values)

    # ---------------------------------------------------------------- prompt
    def add_prompt_attempt(self, messages: list[dict[str, str]], raw_output: str | None,
                           error: str | None = None) -> None:
        """Called by generate_with_retry for every model call, with the exact messages."""
        prompt = self.record["prompt"]
        if prompt is None:
            prompt = self.record["prompt"] = {
                "messages": messages, "sha256": prompt_sha256(messages), "attempts": []}
        prompt["attempts"].append({"attempt": len(prompt["attempts"]) + 1,
                                   "messages_sha256": prompt_sha256(messages),
                                   "extra_messages": messages[len(prompt["messages"]):],
                                   "raw_output": raw_output,
                                   **({"error": error} if error else {})})

    # ---------------------------------------------------------------- output
    def finish(self, status: str, trace_dir: str = "", to_stdout: bool = True) -> dict:
        stages = self.record["stages"]
        total = round((time.perf_counter() - self._t0) * 1000, 2)
        self.record["status"] = status
        self.record["latency_ms"] = {**{n: s["ms"] for n, s in stages.items()
                                        if s.get("ms") is not None}, "total": total}
        errors: list[Exception] = []
        # The file first, as UTF-8 (readable, and the copy the report scripts use). Then stdout
        # as ASCII-escaped JSON: still one valid JSON line, but safe on any console or
        # redirected encoding (a Windows process redirected to a file is cp1252 and cannot
        # print, for example, a non-breaking hyphen). One sink failing never loses the other.
        if trace_dir:
            try:
                line = json.dumps(self.record, ensure_ascii=False, default=str)
                # One file per pod and day: replicas sharing the volume never append to the
                # same file. Kubernetes sets HOSTNAME to the pod name.
                pod = os.environ.get("HOSTNAME", "")
                name = f"traces-{self.record['timestamp'][:10]}{'-' + pod if pod else ''}.jsonl"
                path = Path(trace_dir) / name
                with _file_lock:
                    os.makedirs(trace_dir, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line + chr(10))
            except Exception as exc:  # noqa: BLE001 - reported below, after stdout is tried
                errors.append(exc)
        if to_stdout:
            try:
                sys.stdout.write(json.dumps(self.record, ensure_ascii=True, default=str) + chr(10))
                sys.stdout.flush()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise errors[0]
        return self.record


def iter_traces(trace_dir: str | Path):
    """Every record in TRACE_DIR, oldest file first (used by the report tools and tests)."""
    for path in sorted(Path(trace_dir).glob("traces-*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)
