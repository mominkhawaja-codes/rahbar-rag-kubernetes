"""Smoke test of Part A2 against a real Chroma server (not part of the pytest suite).

Isolated: its own temp folders and its own collection ("a2_smoke_test"), deleted at the end.
Embeddings are the deterministic fake of tests/test_incremental.py, so no Ollama is needed.

    chroma run --path <empty temp folder> --port 8765        # Chroma 1.0.12, in a second terminal
    cd indexer && python tests/integration/chroma_smoke.py 8765

Saved output of the run on 27 Nov 2026: eval/results/a2-incremental/chroma-smoke-20261127.txt
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))   # indexer/
sys.path.insert(0, str(HERE.parent))       # indexer/tests/

import chromadb  # noqa: E402
from chromadb.config import Settings as ChromaSettings  # noqa: E402
from pipeline.config import get_settings  # noqa: E402
from pipeline.incremental import count_chunks, ingest, set_document_status  # noqa: E402
from test_incremental import Embedder, fake_vector, write_doc  # noqa: E402

PORT = int(sys.argv[1])
HOST = "localhost"
tmp = Path(tempfile.mkdtemp(prefix="a2smoke_"))
lake = tmp / "lake"
lake.mkdir()
settings = dataclasses.replace(
    get_settings(), corpus_dir=str(lake), quarantine_dir=str(tmp / "q"),
    report_dir=str(tmp / "r"), state_dir=str(tmp / "s"), archive_dir=str(tmp / "a"),
    chunk_size=40, chunk_overlap=5, min_chars=100, collection="a2_smoke_test",
    chroma_host=HOST, chroma_port=PORT)
client = chromadb.HttpClient(host=HOST, port=PORT,
                             settings=ChromaSettings(anonymized_telemetry=False))
embedder = Embedder()
for name, topic in (("a.md", "attendance"), ("b.md", "grading"), ("c.md", "hostel")):
    write_doc(lake, name, topic)


def run(label: str) -> tuple[dict, object]:
    result = ingest(settings, client=client, embed_fn=embedder)
    collection = client.get_collection(settings.collection)
    docs = {k: v for k, v in result["documents"].items() if v and k != "total"}
    chunks = {k: v for k, v in result["chunks"].items() if k != "by_document"}
    print(f"{label:28} mode={result['mode']:11} docs={docs} chunks={chunks} "
          f"verif_emb={result['verification_embeddings']} "
          f"index_id={collection.metadata.get('index_id')} "
          f"space={collection.metadata.get('hnsw:space')} changed={result['index_id_changed']}")
    return result, collection


def active_sources() -> list[str]:
    got = client.get_collection(settings.collection).get(where={"superseded": False},
                                                         include=["metadatas"])
    return sorted({m["source"] for m in got["metadatas"]})


run("1 first run")
run("2 unchanged rerun")
col = client.get_collection(settings.collection)
print("   collection config space:", (col.configuration or {}).get("hnsw", {}))
stored = [float(x) for x in col.get(limit=1, include=["embeddings"])["embeddings"][0]]
distance = col.query(query_embeddings=[[2 * x for x in stored]], n_results=1,
                     include=["distances"])["distances"][0][0]
print("   distance of 2*stored vector (0 => cosine, large => L2):", round(distance, 6))
write_doc(lake, "d.md", "library")
run("3 add d")
write_doc(lake, "b.md", "grading", sections=2)
run("4 modify b")
(lake / "c.md").unlink()
_, col = run("5 delete c")
print("   count by source c.md:", count_chunks(col, "c.md"), "| sources:",
      sorted({m["source"] for m in col.get(include=["metadatas"])["metadatas"]}))
write_doc(lake, "a-old.md", "scholarship", version="2023", doc_id="att")
write_doc(lake, "a-new.md", "scholarship", version="2026", doc_id="att", sections=3)
run("6 add two versions")
print("   active sources:", active_sources())
restored = set_document_status(settings, "a-old.md", "active", client=client)
print("   restore a-old:", restored["changes"])
print("   active sources:", active_sources())
run("7 rerun after restore")
hits = client.get_collection(settings.collection).query(
    query_embeddings=[fake_vector("x")], n_results=3, where={"superseded": False},
    include=["metadatas"])
print("   query with superseded filter ok:", [m["source"] for m in hits["metadatas"][0]])
client.delete_collection(settings.collection)
print("cleaned up test collection; temp:", tmp)
