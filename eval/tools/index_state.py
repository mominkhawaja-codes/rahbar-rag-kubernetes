#!/usr/bin/env python3
"""Print the Chroma collections and the live index's identity as JSON.

    CHROMA_HOST=localhost CHROMA_PORT=8000 python eval/tools/index_state.py

Used to check what a failed indexing run leaves behind: the live collection `rahbar`
should keep its index_id and count, with at most a leftover `rahbar__staging`.
"""

from __future__ import annotations

import json
import os


def main() -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.HttpClient(host=os.environ.get("CHROMA_HOST", "localhost"),
                                 port=int(os.environ.get("CHROMA_PORT", "8000")),
                                 settings=Settings(anonymized_telemetry=False))
    names = sorted(c if isinstance(c, str) else c.name for c in client.list_collections())
    state: dict = {"collections": names}
    live = os.environ.get("CHROMA_COLLECTION", "rahbar")
    if live in names:
        col = client.get_collection(live)
        meta = col.metadata or {}
        state["live"] = {"count": col.count(), "index_id": meta.get("index_id"),
                         "built_at": meta.get("built_at"), "embed_model": meta.get("embed_model")}
    print(json.dumps(state))


if __name__ == "__main__":
    main()
