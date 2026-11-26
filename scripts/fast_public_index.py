#!/usr/bin/env python3
"""Export or seed the verified FAST public snapshot through a local Chroma port-forward."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

COLLECTION = "rahbar-fast-public"
COLLECTIONS = "/api/v2/tenants/default_tenant/databases/default_database/collections"
FIELDS = ("ids", "embeddings", "documents", "metadatas")
BATCH_SIZE = 100
CHUNKS = 612
DIMENSION = 768


def request(port, path, data=None, *, method=None):
    payload = json.dumps(data, allow_nan=False).encode() if data is not None else None
    req = Request(f"http://127.0.0.1:{port}{path}", data=payload, method=method,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=45) as response:
        body = response.read()
        return json.loads(body) if body else None


def collection_path(collection):
    return f"{COLLECTIONS}/{collection['id']}"


def read_bundle(port, collection):
    path = collection_path(collection)
    if request(port, path + "/count") != CHUNKS:
        raise ValueError(f"Collection {collection['name']} must contain {CHUNKS} chunks")
    batches = [request(port, path + "/get", {"offset": offset, "limit": BATCH_SIZE,
                                            "include": list(FIELDS[1:])})
               for offset in range(0, CHUNKS, BATCH_SIZE)]
    return {"collection": collection, "batches": batches}


def snapshot_records(bundle):
    collection = bundle["collection"]
    metadata = collection["metadata"]
    if (metadata["embed_model"] != "nomic-embed-text" or metadata["dimension"] != DIMENSION
            or collection["dimension"] != DIMENSION or not metadata["index_id"]
            or collection["configuration_json"]["hnsw"]["space"] != "cosine"):
        raise ValueError("Snapshot model, dimension, index ID or distance settings do not match")

    records = {}
    for batch in bundle["batches"]:
        for chunk_id, vector, text, fields in zip(*(batch[key] for key in FIELDS), strict=True):
            if (chunk_id in records or len(vector) != DIMENSION or not all(map(math.isfinite, vector))
                    or not text or not {"source", "section", "superseded"} <= fields.keys()):
                raise ValueError(f"Invalid or duplicate chunk {chunk_id}")
            records[chunk_id] = (vector, text, fields)
    if len(records) != CHUNKS or len({fields["source"] for _, _, fields in records.values()}) != 9:
        raise ValueError("Expected 612 chunks across nine documents")
    return records


def verify(port, target, source, expected):
    observed = snapshot_records(read_bundle(port, target))
    if (observed.keys() != expected.keys() or target["metadata"] != source["metadata"]
            or target["configuration_json"] != source["configuration_json"]):
        raise ValueError("Destination IDs, metadata or settings differ from the snapshot")
    for chunk_id, (vector, text, fields) in expected.items():
        stored_vector, stored_text, stored_fields = observed[chunk_id]
        if (text != stored_text or fields != stored_fields
                or any(abs(a - b) > 1e-6 for a, b in zip(vector, stored_vector, strict=True))):
            raise ValueError(f"Destination record differs at {chunk_id}")


def seed(port, bundle):
    source = bundle["collection"]
    expected = snapshot_records(bundle)
    try:
        target = request(port, f"{COLLECTIONS}/{COLLECTION}")
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        verify(port, target, source, expected)
        print(f"{COLLECTION} already matches the snapshot. No writes needed.")
        return

    staging_name = f"{COLLECTION}-seed-{uuid4().hex[:8]}"
    target = request(port, COLLECTIONS, {
        "name": staging_name,
        "metadata": source["metadata"],
        "configuration": source["configuration_json"],
        "get_or_create": False,
    })
    print(f"Uploading stored vectors to staging collection {staging_name}", flush=True)
    path = collection_path(target)
    for batch in bundle["batches"]:
        request(port, path + "/add", {key: batch[key] for key in FIELDS})
    verify(port, request(port, f"{COLLECTIONS}/{staging_name}"), source, expected)
    request(port, path, {"new_name": COLLECTION}, method="PUT")
    if request(port, f"{COLLECTIONS}/{COLLECTION}")["id"] != target["id"]:
        raise ValueError("The published collection does not match the verified staging collection")
    print(f"Published {COLLECTION} with {CHUNKS} verified chunks.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "seed"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--port", type=int, default=18000, help="local port-forward port")
    args = parser.parse_args()
    try:
        if args.action == "export":
            bundle = read_bundle(args.port, request(args.port, f"{COLLECTIONS}/{COLLECTION}"))
            snapshot_records(bundle)
            with args.bundle.open("x", encoding="utf-8") as output:
                json.dump(bundle, output, ensure_ascii=False, allow_nan=False)
            print(f"Exported {CHUNKS} public chunks to {args.bundle}")
        else:
            seed(args.port, json.loads(args.bundle.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAST index transfer failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
