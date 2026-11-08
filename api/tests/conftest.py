"""Shared fakes for the API tests. Nothing here touches a network, a model or a cluster."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.schemas import Answer, Citation, Passage


def passage(n: int, score: float, source: str = "rules.pdf", section: str | None = None,
            text: str | None = None) -> Passage:
    return Passage(chunk_id=f"c{n}", text=text or f"Passage {n} text.", source=source,
                   section=section or f"Section {n} [{n}.1]", page=n, score=score)


class FixedRetriever:
    name = "fixed"

    def __init__(self, passages: list[Passage]) -> None:
        self.passages = passages
        self.calls = 0

    def retrieve(self, question: str, k: int) -> list[Passage]:
        self.calls += 1
        return self.passages[:k]


class ScriptedProvider:
    """Returns the given answers in order, or raises the given exceptions."""

    name = "scripted"

    def __init__(self, *results: Answer | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, list[Passage]]] = []

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        self.calls.append((question, passages))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def grounded_answer(*cites: Passage, confidence: str = "high") -> Answer:
    return Answer(reasoning="r", answer="The rule says so.",
                  citations=[Citation(source=p.source, section=p.section, page=p.page)
                             for p in cites],
                  answered_from_context=True, confidence=confidence)  # type: ignore[arg-type]


@pytest.fixture()
def settings() -> Settings:
    return Settings(relevance_floor=0.5, cache_backend="memory")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from app.main import app

    with TestClient(app) as c:
        yield c


def swap_state(client: TestClient, **attrs: Any) -> None:
    for key, value in attrs.items():
        setattr(client.app.state, key, value)  # type: ignore[attr-defined]
