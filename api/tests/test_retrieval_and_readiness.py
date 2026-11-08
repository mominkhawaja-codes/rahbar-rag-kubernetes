"""ChromaRetriever against a fake Chroma client, the embedding pin, and /readyz."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import swap_state
from fastapi.testclient import TestClient

from app.config import Settings
from app.retrieval.chroma import (ChromaRetriever, EmbeddingPinMismatch, pin_problem,
                                  wants_history)


class FakeEmbedder:
    model = "nomic-embed-text"

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        return [0.5] * self.dim


class FakeCollection:
    def __init__(self, metadata: dict[str, Any], count: int = 2) -> None:
        self.metadata = metadata
        self._count = count
        self.queries: list[dict[str, Any]] = []

    def count(self) -> int:
        return self._count

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"ids": [["a-0001", "a-0002"]],
                "documents": [["5.4 Minimum CGPA ... 2.00", "5.5 If CGPA drops ..."]],
                "metadatas": [[{"source": "rules.pdf", "section": "Warning [5.4]", "page": 24,
                                "doc_version": "2026", "superseded": False},
                               {"source": "rules.pdf", "section": "Warning [5.5]", "page": 24,
                                "doc_version": "2026", "superseded": False}]],
                "distances": [[0.2, 0.35]]}


class FakeClient:
    def __init__(self, collection: FakeCollection) -> None:
        self.c = collection

    def get_collection(self, name: str) -> FakeCollection:
        return self.c


def make(meta: dict[str, Any], dim: int = 4) -> tuple[ChromaRetriever, FakeCollection]:
    col = FakeCollection(meta)
    settings = Settings(retriever="chroma", embed_model="nomic-embed-text")
    return ChromaRetriever(settings, embedder=FakeEmbedder(dim), client=FakeClient(col)), col


GOOD = {"embed_model": "nomic-embed-text", "dimension": 4, "index_id": "abc"}


def test_superseded_documents_are_filtered_and_scores_are_similarities() -> None:
    retriever, col = make(GOOD)
    passages = retriever.retrieve("What CGPA avoids academic warning?", k=5)
    assert col.queries[0]["where"] == {"superseded": False}
    assert col.queries[0]["n_results"] == 5
    assert [p.score for p in passages] == [0.8, 0.65]          # 1 - distance
    assert passages[0].section == "Warning [5.4]" and passages[0].page == 24


def test_questions_about_old_versions_may_see_superseded_documents() -> None:
    assert wants_history("What was the attendance rule in the previous version?")
    assert wants_history("What did the 2023 rules say about warnings?")
    assert not wants_history("What is the attendance rule?")
    retriever, col = make(GOOD)
    retriever.retrieve("What was the old attendance rule?", k=3)
    assert col.queries[0]["where"] is None


def test_embedding_model_mismatch_is_refused() -> None:
    assert pin_problem({"embed_model": "dummy-768"}, "nomic-embed-text") is not None
    assert "mismatch" in (pin_problem({"embed_model": "dummy-768"}, "nomic-embed-text") or "")
    assert pin_problem({}, "nomic-embed-text") is not None
    assert pin_problem(GOOD, "nomic-embed-text") is None
    retriever, _ = make({"embed_model": "dummy-768", "dimension": 768})
    with pytest.raises(EmbeddingPinMismatch):
        retriever.retrieve("anything", k=3)


def test_embedding_dimension_mismatch_is_refused() -> None:
    retriever, _ = make({**GOOD, "dimension": 768}, dim=4)
    with pytest.raises(EmbeddingPinMismatch):
        retriever.retrieve("anything", k=3)


def test_index_id_comes_from_the_collection_metadata() -> None:
    retriever, _ = make(GOOD)
    assert retriever.index_id() == "abc"


class Unhealthy:
    name = "chroma"

    def __init__(self, checks: dict[str, dict[str, Any]]) -> None:
        self.checks = checks

    def retrieve(self, question: str, k: int) -> list:
        raise EmbeddingPinMismatch("embedding model mismatch: index built with 'dummy-768'")

    def readiness_checks(self) -> dict[str, dict[str, Any]]:
        return self.checks


def test_readyz_is_ready_with_stubs(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_readyz_names_the_failed_dependency(client: TestClient) -> None:
    swap_state(client, retriever=Unhealthy({
        "chroma": {"ok": True}, "collection": {"ok": False, "error": "collection is empty"},
        "embed_pin": {"ok": True}}))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["failed"] == ["collection"]


def test_readyz_fails_when_the_pin_does_not_match(client: TestClient) -> None:
    swap_state(client, retriever=Unhealthy({
        "chroma": {"ok": True}, "collection": {"ok": True, "count": 3},
        "embed_pin": {"ok": False, "error": "embedding model mismatch"}}))
    assert client.get("/readyz").json()["failed"] == ["embed_pin"]


def test_readyz_checks_redis_when_redis_is_the_cache(client: TestClient) -> None:
    class DownRedis:
        name = "redis"

        def ping(self) -> bool:
            raise ConnectionError("connection refused")

    swap_state(client, cache=DownRedis())
    r = client.get("/readyz")
    assert r.status_code == 503 and "redis" in r.json()["failed"]


def test_liveness_stays_up_when_dependencies_are_down(client: TestClient) -> None:
    swap_state(client, retriever=Unhealthy({"chroma": {"ok": False, "error": "down"}}))
    assert client.get("/healthz").status_code == 200


def test_ask_answers_503_not_500_on_a_dependency_failure(client: TestClient) -> None:
    swap_state(client, retriever=Unhealthy({}))
    r = client.post("/api/ask", json={"question": "What is the attendance rule?"},
                    headers={"X-Request-ID": "pin-1"})
    assert r.status_code == 503
    assert r.json()["request_id"] == "pin-1"
    assert "embed_pin" in r.json()["detail"]
