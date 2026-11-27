"""Part A2 with an in-memory stand-in for Chroma and a deterministic fake embedder: unchanged,
added, modified, deleted, rejected, superseded and restored documents. No server, no model."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import IndexerSettings, get_settings
from pipeline.incremental import (
    ACTIVE,
    ARCHIVED,
    count_chunks,
    ingest,
    load_state,
    restore_from_archive,
    set_document_status,
)


# ---------------------------------------------------------------- fake chroma
class FakeCollection:
    def __init__(self, client: FakeClient, name: str, metadata: dict[str, Any]) -> None:
        self.client, self.name, self.metadata = client, name, dict(metadata)
        self.rows: dict[str, dict[str, Any]] = {}

    def add(self, ids, documents, embeddings, metadatas) -> None:
        self.upsert(ids, documents, embeddings, metadatas)

    def upsert(self, ids, documents, embeddings, metadatas) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas, strict=True):
            self.rows[i] = {"doc": d, "vec": list(e), "meta": dict(m)}

    def update(self, ids, metadatas) -> None:
        for i, m in zip(ids, metadatas, strict=True):
            self.rows[i]["meta"].update(m)             # Chroma merges metadata keys

    def _match(self, row: dict[str, Any], where: dict[str, Any] | None) -> bool:
        return all(row["meta"].get(k) == v for k, v in (where or {}).items())

    def delete(self, ids=None, where=None) -> None:
        for i in [i for i, r in self.rows.items()
                  if (ids is None or i in ids) and self._match(r, where)]:
            del self.rows[i]

    def get(self, where=None, include=None) -> dict[str, list]:
        hits = [(i, r) for i, r in self.rows.items() if self._match(r, where)]
        return {"ids": [i for i, _ in hits], "metadatas": [dict(r["meta"]) for _, r in hits]}

    def count(self) -> int:
        return len(self.rows)

    def modify(self, name: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        if metadata is not None:
            self.metadata = dict(metadata)
        if name:
            self.client.collections.pop(self.name)
            self.name = name
            self.client.collections[name] = self


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def get_collection(self, name: str) -> FakeCollection:
        return self.collections[name]

    def create_collection(self, name: str, metadata: dict[str, Any]) -> FakeCollection:
        self.collections[name] = FakeCollection(self, name, metadata)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        del self.collections[name]


def fake_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [(b - 128) / 128 for b in digest]


class Embedder:
    """Counts every text it is asked to embed, for chunks and for verification alike."""

    def __init__(self) -> None:
        self.texts = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.texts += len(texts)
        return [fake_vector(t) for t in texts]


# ---------------------------------------------------------------- helpers
def write_doc(lake: Path, name: str, topic: str, sections: int = 4, version: str = "2026",
              doc_id: str | None = None, superseded: bool = False,
              title: str | None = None) -> Path:
    meta = [f"doc_version: {version}", f"superseded: {str(superseded).lower()}"]
    if doc_id:
        meta.append(f"doc_id: {doc_id}")
    if title:
        meta.append(f"title: {title}")
    body = [f"# {topic.title()} Rules\n"] if title is None else []
    for n in range(1, sections + 1):
        body.append(f"## {n}. {topic} part {n}\n\n{n}.1 The {topic} office handles case {n} "
                    f"of the {topic} policy and records every decision about {topic} number "
                    f"{n} in writing for the student, the department head and the registrar "
                    f"office of the institute.\n")
    path = lake / name
    path.write_text("---\n" + "\n".join(meta) + "\n---\n" + "\n".join(body), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path: Path):
    lake = tmp_path / "lake"
    lake.mkdir()
    settings: IndexerSettings = dataclasses.replace(
        get_settings(), corpus_dir=str(lake), quarantine_dir=str(tmp_path / "quarantine"),
        report_dir=str(tmp_path / "reports"), state_dir=str(tmp_path / "state"),
        archive_dir=str(tmp_path / "archive"), chunk_size=40, chunk_overlap=5, min_chars=100)
    client = FakeClient()
    return lake, settings, client, Embedder()


def run(env, **kw) -> dict[str, Any]:
    _, settings, client, embedder = env
    return ingest(settings, client=client, embed_fn=embedder, **kw)


def live(env) -> FakeCollection:
    return env[2].collections[env[1].collection]


def three_documents(lake: Path) -> None:
    for name, topic in (("a.md", "attendance"), ("b.md", "grading"), ("c.md", "hostel")):
        write_doc(lake, name, topic)


# ---------------------------------------------------------------- tests
def test_unchanged_rerun_embeds_zero_chunks_and_zero_verification_texts(env) -> None:
    lake = env[0]
    three_documents(lake)
    first = run(env)
    assert first["mode"] == "full" and first["chunks"]["embedded"] > 0
    assert sorted(first["documents"]["added"]) == ["a.md", "b.md", "c.md"]
    rows_before = dict(live(env).rows)
    texts_before, id_before = env[3].texts, live(env).metadata["index_id"]

    second = run(env)
    assert second["mode"] == "incremental"
    assert second["chunks"]["embedded"] == 0 and second["verification_embeddings"] == 0
    assert second["chunks"]["upserted"] == 0 and second["chunks"]["deleted"] == 0
    assert second["documents"]["unchanged"] == ["a.md", "b.md", "c.md"]
    assert env[3].texts == texts_before                      # the embedder was never called
    assert live(env).rows == rows_before
    assert live(env).metadata["index_id"] == id_before       # the cache generation is kept
    assert second["index_id_changed"] is False


def test_added_document_embeds_only_its_own_chunks(env) -> None:
    lake = env[0]
    three_documents(lake)
    run(env)
    before = {k: dict(v) for k, v in live(env).rows.items()}
    id_before, texts_before = live(env).metadata["index_id"], env[3].texts

    write_doc(lake, "d.md", "library")
    result = run(env)
    assert result["documents"]["added"] == ["d.md"]
    new_chunks = count_chunks(live(env), "d.md")
    assert new_chunks > 0 and result["chunks"]["embedded"] == new_chunks
    assert {k: v for k, v in live(env).rows.items() if k in before} == before   # others intact
    assert live(env).metadata["index_id"] != id_before and result["index_id_changed"]
    # chunks of d.md, plus the verification windows of d.md alone (it fits one window)
    assert env[3].texts - texts_before == new_chunks + result["verification_embeddings"]
    assert result["verification_embeddings"] == 1


def test_modified_document_replaces_only_its_own_chunks(env) -> None:
    lake = env[0]
    three_documents(lake)
    run(env)
    old_b = count_chunks(live(env), "b.md")
    others = {k: dict(v) for k, v in live(env).rows.items() if v["meta"]["source"] != "b.md"}

    write_doc(lake, "b.md", "grading", sections=2)           # shorter: stale chunks must go
    result = run(env)
    new_b = count_chunks(live(env), "b.md")
    assert result["documents"]["changed"] == ["b.md"] and new_b < old_b
    assert result["chunks"]["embedded"] == new_b
    assert result["chunks"]["deleted"] == old_b - new_b
    assert {k: v for k, v in live(env).rows.items()
            if v["meta"]["source"] != "b.md"} == others
    assert load_state(env[1].state_dir).documents["b.md"].chunk_ids == sorted(
        k for k, v in live(env).rows.items() if v["meta"]["source"] == "b.md")


def test_deleted_document_leaves_no_orphan_chunks(env) -> None:
    lake = env[0]
    three_documents(lake)
    run(env)
    assert count_chunks(live(env), "c.md") > 0
    id_before = live(env).metadata["index_id"]

    (lake / "c.md").unlink()
    result = run(env)
    assert result["documents"]["deleted"] == ["c.md"]
    assert count_chunks(live(env), "c.md") == 0              # counted by document, not assumed
    assert {v["meta"]["source"] for v in live(env).rows.values()} == {"a.md", "b.md"}
    assert "c.md" not in load_state(env[1].state_dir).documents
    assert result["chunks"]["embedded"] == 0 and live(env).metadata["index_id"] != id_before


def test_leftover_chunks_of_a_failed_earlier_run_are_removed(env) -> None:
    lake = env[0]
    three_documents(lake)
    run(env)
    live(env).upsert(["ghost-0000"], ["x"], [[0.0]], [{"source": "ghost.md", "superseded": False}])
    result = run(env)
    assert result["orphans_removed"] == 1 and count_chunks(live(env), "ghost.md") == 0


def test_rejected_documents_never_enter_the_index(env) -> None:
    lake, settings = env[0], env[1]
    three_documents(lake)
    run(env)
    ids = set(live(env).rows)
    (lake / "dup.md").write_bytes((lake / "a.md").read_bytes())            # exact duplicate
    write_doc(lake, "nometa.md", "transport", title="")                    # no title
    (lake / "nometa.md").write_text(
        "---\nsource: nometa.md\n---\n" + "The transport office runs buses every day. " * 12,
        encoding="utf-8")
    result = run(env)
    assert sorted(result["documents"]["rejected"]) == ["dup.md", "nometa.md"]
    assert set(live(env).rows) == ids and result["chunks"]["embedded"] == 0
    assert (Path(settings.quarantine_dir) / "dup.md").exists()
    assert result["index_id_changed"] is False
    assert {"dup.md", "nometa.md"}.isdisjoint(load_state(settings.state_dir).documents)


def test_a_known_document_replaced_by_a_rejected_version_loses_its_chunks(env) -> None:
    lake = env[0]
    three_documents(lake)
    run(env)
    (lake / "b.md").write_text("---\nsource: b.md\n---\nToo short.\n", encoding="utf-8")
    result = run(env)
    assert result["documents"]["deleted"] == ["b.md"] and "b.md" in result["documents"]["rejected"]
    assert count_chunks(live(env), "b.md") == 0


def test_newer_version_archives_the_old_one_and_restore_brings_it_back(env) -> None:
    lake, settings = env[0], env[1]
    write_doc(lake, "rules-2023.md", "attendance", version="2023", doc_id="rules")
    run(env)
    old_chunks = count_chunks(live(env), "rules-2023.md")
    assert not any(r["meta"]["superseded"] for r in live(env).rows.values())

    write_doc(lake, "rules-2026.md", "attendance", version="2026", doc_id="rules", sections=3)
    result = run(env)
    new_chunks = count_chunks(live(env), "rules-2026.md")
    assert result["documents"]["newly_archived"] == ["rules-2023.md"]
    assert result["chunks"]["embedded"] == new_chunks                    # old one not re-embedded
    assert result["chunks"]["metadata_updated"] == old_chunks
    active = live(env).get(where={"superseded": False})["metadatas"]
    assert {m["source"] for m in active} == {"rules-2026.md"}            # excluded from retrieval
    assert count_chunks(live(env), "rules-2023.md") == old_chunks        # but not deleted
    assert result["archived_documents"] == ["rules-2023.md"]
    assert (Path(settings.archive_dir) / "rules" / "2023" / "rules-2023.md").exists()

    texts, id_before = env[3].texts, live(env).metadata["index_id"]
    back = set_document_status(settings, "rules-2023.md", ACTIVE, client=env[2])
    assert back["changes"] == {"rules-2023.md": ACTIVE, "rules-2026.md": ARCHIVED}
    assert env[3].texts == texts                                         # nothing embedded
    active = live(env).get(where={"superseded": False})["metadatas"]
    assert {m["source"] for m in active} == {"rules-2023.md"}
    assert live(env).metadata["index_id"] != id_before

    again = run(env)                                                     # the choice is kept
    assert again["chunks"]["embedded"] == 0 and again["documents"]["metadata_updated"] == []
    assert load_state(settings.state_dir).documents["rules-2023.md"].status == ACTIVE
    assert again["index_id_changed"] is False

    set_document_status(settings, "rules-2026.md", ACTIVE, client=env[2])   # roll forward again
    active = live(env).get(where={"superseded": False})["metadatas"]
    assert {m["source"] for m in active} == {"rules-2026.md"}


def test_declared_superseded_document_is_archived_from_the_start(env) -> None:
    lake = env[0]
    write_doc(lake, "old.md", "attendance", version="2023", superseded=True)
    write_doc(lake, "new.md", "grading")
    result = run(env)
    assert result["archived_documents"] == ["old.md"]
    assert {m["source"] for m in live(env).get(where={"superseded": False})["metadatas"]} == {
        "new.md"}


def test_changing_the_embedding_model_forces_a_full_rebuild(env) -> None:
    lake, settings, client, embedder = env
    three_documents(lake)
    run(env)
    switched = dataclasses.replace(settings, embed_model="other-model")
    result = ingest(switched, client=client, embed_fn=embedder)
    assert result["mode"] == "full" and "embed_model changed" in result["reason"]
    assert result["chunks"]["embedded"] == result["chunks"]["total_after"]


def test_dimension_mismatch_is_refused_not_mixed_in(env) -> None:
    lake, settings, client, _ = env
    three_documents(lake)
    run(env)
    write_doc(lake, "d.md", "library")
    with pytest.raises(RuntimeError, match="dimension"):
        ingest(settings, client=client, embed_fn=lambda texts: [
            [0.1, 0.2] if " | " in t else fake_vector(t) for t in texts])   # chunks only


@pytest.mark.parametrize("failing_call", ["upsert", "modify"])
def test_interrupted_update_is_completed_by_the_next_run(env, failing_call: str) -> None:
    """The store can be half-updated when a run dies: the state file is only written after the
    store is verified, so the next run redoes the work idempotently and ends consistent."""
    lake, settings = env[0], env[1]
    three_documents(lake)
    run(env)
    id_before = live(env).metadata["index_id"]
    write_doc(lake, "b.md", "grading", sections=2)               # modified
    (lake / "c.md").unlink()                                     # deleted

    collection, real = live(env), getattr(live(env), failing_call)

    def dies(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("chroma went away")

    setattr(collection, failing_call, dies)
    with pytest.raises(ConnectionError):
        run(env)
    # the run died after part of the work: the store is ahead of the state file
    assert set(load_state(settings.state_dir).documents) == {"a.md", "b.md", "c.md"}
    assert count_chunks(collection, "c.md") == 0

    setattr(collection, failing_call, real)
    result = run(env)
    assert result["documents"]["changed"] == ["b.md"] and result["documents"]["deleted"] == ["c.md"]
    state = load_state(settings.state_dir)
    assert set(state.documents) == {"a.md", "b.md"}
    for name, doc in state.documents.items():                    # store == state, no orphans
        assert count_chunks(collection, name) == len(doc.chunk_ids)
    assert {r["meta"]["source"] for r in collection.rows.values()} == {"a.md", "b.md"}
    assert collection.metadata["index_id"] != id_before          # caches are invalidated
    assert run(env)["chunks"]["embedded"] == 0                   # and the next run is a no-op


def _archive_the_old_version(env) -> None:
    lake = env[0]
    write_doc(lake, "rules-2023.md", "attendance", version="2023", doc_id="rules")
    run(env)
    write_doc(lake, "rules-2026.md", "attendance", version="2026", doc_id="rules", sections=3)
    run(env)


def test_archived_version_is_restored_from_the_archive_when_its_lake_file_is_gone(env) -> None:
    lake, settings, client, _ = env
    _archive_the_old_version(env)
    archived = Path(settings.archive_dir) / "rules" / "2023" / "rules-2023.md"
    assert archived.exists()

    (lake / "rules-2023.md").unlink()                            # the old file leaves the lake
    gone = run(env)
    assert gone["documents"]["deleted"] == ["rules-2023.md"]
    assert count_chunks(live(env), "rules-2023.md") == 0         # chunks go, the archive stays
    assert archived.exists()

    result = restore_from_archive(settings, "rules-2023.md", client=client, embed_fn=env[3])
    assert result["from_archive"] is True and (lake / "rules-2023.md").exists()
    assert result["ingest"]["embedded"] == count_chunks(live(env), "rules-2023.md") > 0
    active = live(env).get(where={"superseded": False})["metadatas"]
    assert {m["source"] for m in active} == {"rules-2023.md"}    # the old version is the live one
    assert load_state(settings.state_dir).documents["rules-2026.md"].status == ARCHIVED
    assert run(env)["chunks"]["embedded"] == 0                   # and it stays that way


def test_restore_from_archive_re_lists_the_file_in_index_json(env) -> None:
    lake, settings, client, _ = env
    write_doc(lake, "rules-2023.md", "attendance", version="2023", doc_id="rules")
    write_doc(lake, "rules-2026.md", "attendance", version="2026", doc_id="rules", sections=3)
    entries = [{"name": "Rules", "file": n, "doc_version": v, "doc_id": "rules"}
               for n, v in (("rules-2023.md", "2023"), ("rules-2026.md", "2026"))]
    index = lake / "_index.json"
    index.write_text(json.dumps(entries), encoding="utf-8")
    run(env)
    (lake / "rules-2023.md").unlink()
    index.write_text(json.dumps(entries[1:]), encoding="utf-8")   # removed from the index too
    run(env)

    restore_from_archive(settings, "rules-2023.md", client=client, embed_fn=env[3])
    assert "rules-2023.md" in {e["file"] for e in json.loads(index.read_text(encoding="utf-8"))}
    assert count_chunks(live(env), "rules-2023.md") > 0


def test_restore_of_an_unknown_file_says_so(env) -> None:
    lake, settings, client, _ = env
    three_documents(lake)
    run(env)
    with pytest.raises(SystemExit, match="neither indexed nor in the archive"):
        restore_from_archive(settings, "nothing.md", client=client)
