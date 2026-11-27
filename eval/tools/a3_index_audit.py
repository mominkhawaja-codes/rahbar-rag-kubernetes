"""Audit an index against the indexer's state file (Assignment 03, Parts A1 and A2).

Reads Chroma and `index-state.json` and reports, from the store itself:

* chunks per source, whether each chunk is flagged superseded, and the collection's `index_id`;
* orphans (chunks whose source the state does not know) and missing documents (in the state,
  no chunks): the run is consistent only if there are none;
* optionally a manifest of every chunk (id, source, superseded flag, md5 of its embedding), and a
  comparison with an earlier manifest, which shows which chunks were reused untouched;
* optionally what a question retrieves, with and without the `superseded == False` filter.

    python eval/tools/a3_index_audit.py --port 8801 --state-dir STATE --label batch-a \\
        --out audit.json --manifest manifest.json
    python eval/tools/a3_index_audit.py --port 8802 --state-dir STATE --compare manifest.json
    python eval/tools/a3_index_audit.py --port 8802 --state-dir STATE --query "How long ..."
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import chromadb
import httpx
from chromadb.config import Settings as ChromaSettings


def vector_md5(vector: list[float]) -> str:
    return hashlib.md5(",".join(f"{x:.7g}" for x in vector).encode()).hexdigest()


def audit(collection, state: dict) -> dict:
    got = collection.get(include=["metadatas"])
    per_source: dict[str, dict] = {}
    for meta in got["metadatas"]:
        row = per_source.setdefault(meta["source"], {"chunks": 0, "superseded": set(),
                                                     "doc_ids": set()})
        row["chunks"] += 1
        row["superseded"].add(bool(meta.get("superseded")))
        row["doc_ids"].add(meta.get("doc_id"))
    docs = state.get("documents", {})
    orphans = sorted(set(per_source) - set(docs))
    missing = sorted(n for n in docs if n not in per_source)
    wrong = {n: {"store": per_source[n]["chunks"], "state": len(d["chunk_ids"])}
             for n, d in docs.items() if n in per_source
             and per_source[n]["chunks"] != len(d["chunk_ids"])}
    flag_mismatch = sorted(n for n, d in docs.items() if n in per_source
                           and per_source[n]["superseded"] != {d["status"] == "archived"})
    meta = dict(collection.metadata or {})
    return {
        "collection": collection.name,
        "index_id": meta.get("index_id"), "built_at": meta.get("built_at"),
        "embed_model": meta.get("embed_model"), "dimension": meta.get("dimension"),
        "total_chunks": int(collection.count()),
        "sources": {n: {"chunks": r["chunks"], "superseded": sorted(r["superseded"]),
                        "doc_ids": sorted(map(str, r["doc_ids"]))}
                    for n, r in sorted(per_source.items())},
        "state": {n: {"status": d["status"], "locked": d.get("locked", False),
                      "doc_id": d["doc_id"], "doc_version": d["doc_version"],
                      "chunks": len(d["chunk_ids"])} for n, d in sorted(docs.items())},
        "active_sources": sorted(n for n, d in docs.items() if d["status"] == "active"),
        "archived_sources": sorted(n for n, d in docs.items() if d["status"] == "archived"),
        "orphans": orphans, "missing_from_store": missing,
        "count_mismatch": wrong, "flag_mismatch": flag_mismatch,
        "consistent": not (orphans or missing or wrong or flag_mismatch),
        "state_chunks": sum(len(d["chunk_ids"]) for d in docs.values()),
    }


def manifest(collection) -> dict:
    got = collection.get(include=["metadatas", "embeddings"])
    return {i: {"source": m["source"], "superseded": bool(m.get("superseded")),
                "doc_id": m.get("doc_id"), "embedding_md5": vector_md5([float(x) for x in e])}
            for i, m, e in zip(got["ids"], got["metadatas"], got["embeddings"], strict=True)}


def compare(before: dict, after: dict) -> dict:
    same = [i for i in before if i in after and before[i]["embedding_md5"] == after[i]["embedding_md5"]
            and before[i]["source"] == after[i]["source"]]
    flag_changed: dict[str, dict] = {}
    for i in same:
        if before[i]["superseded"] != after[i]["superseded"]:
            row = flag_changed.setdefault(before[i]["source"], {"chunks": 0})
            row["chunks"] += 1
            row["superseded"] = f'{before[i]["superseded"]} -> {after[i]["superseded"]}'
    added: dict[str, int] = {}
    for i in after:
        if i not in before:
            added[after[i]["source"]] = added.get(after[i]["source"], 0) + 1
    removed: dict[str, int] = {}
    for i in before:
        if i not in after:
            removed[before[i]["source"]] = removed.get(before[i]["source"], 0) + 1
    return {"chunks_before": len(before), "chunks_after": len(after),
            "chunks_kept_with_identical_id_and_embedding": len(same),
            "embedding_changed_under_same_id": sorted(
                i for i in before if i in after and i not in same),
            "superseded_flag_changed_in_place": flag_changed,
            "added_by_source": added, "removed_by_source": removed}


def retrieve(collection, question: str, ollama: str, model: str, k: int) -> dict:
    prefix = "search_query: " if "nomic" in model else ""
    vec = httpx.post(f"{ollama.rstrip('/')}/api/embed", timeout=120,
                     json={"model": model, "input": [prefix + question]}).json()["embeddings"][0]
    out = {"question": question}
    for label, where in (("active_only (superseded == False)", {"superseded": False}),
                         ("no filter", None)):
        res = collection.query(query_embeddings=[vec], n_results=k, where=where,
                               include=["metadatas", "distances"])
        out[label] = [{"source": m["source"], "section": m["section"], "page": m["page"],
                       "superseded": m["superseded"], "distance": round(d, 4)}
                      for m, d in zip(res["metadatas"][0], res["distances"][0], strict=True)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--collection", default="rahbar")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--manifest", type=Path, help="write a chunk manifest here")
    ap.add_argument("--compare", type=Path, help="compare with an earlier manifest")
    ap.add_argument("--query", help="show what this question retrieves")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-model", default="nomic-embed-text")
    args = ap.parse_args()

    client = chromadb.HttpClient(host=args.host, port=args.port,
                                 settings=ChromaSettings(anonymized_telemetry=False))
    collection = client.get_collection(args.collection)
    state = json.loads((Path(args.state_dir) / "index-state.json").read_text(encoding="utf-8"))
    result = {"label": args.label, **audit(collection, state)}
    if args.compare:
        result["compared_with"] = str(args.compare)
        result["comparison"] = compare(json.loads(args.compare.read_text(encoding="utf-8")),
                                       manifest(collection))
    if args.manifest:
        args.manifest.write_text(json.dumps(manifest(collection), indent=1), encoding="utf-8")
    if args.query:
        result["retrieval"] = retrieve(collection, args.query, args.ollama_url,
                                       args.embed_model, args.k)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
