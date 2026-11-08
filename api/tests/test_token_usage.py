"""Token usage: provider-reported counts reach Prometheus and the per-request log line,
retries are counted, and missing usage is never reported as zero."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from conftest import FixedRetriever, passage
from prometheus_client import REGISTRY

from app.config import Settings
from app.embeddings import HashEmbedder
from app.providers.llm.hosted import HostedProvider
from app.providers.llm.ollama import OllamaProvider
from app.services.answer import answer_question
from app.services.cache import MemoryCache

P = passage(1, 0.9, source="rules.pdf", section="Academic Warning [5.4]",
            text="5.4 Minimum CGPA required to avoid academic warning is 2.00.")
VALID = json.dumps({"reasoning": "r", "answer": "2.00.", "answered_from_context": True,
                    "confidence": "high", "citations": [
                        {"source": P.source, "section": P.section, "page": P.page}]})
QUESTION = "What CGPA avoids academic warning?"


def fake_post(monkeypatch: pytest.MonkeyPatch, module: Any, replies: list[dict]) -> list[dict]:
    calls: list[dict] = []

    def post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json=replies[min(len(calls), len(replies)) - 1],
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(module.httpx, "post", post)
    return calls


def ollama_reply(content: str, prompt: int | None, completion: int | None) -> dict:
    body: dict = {"message": {"content": content}}
    if prompt is not None:
        body["prompt_eval_count"] = prompt
    if completion is not None:
        body["eval_count"] = completion
    return body


def tokens(provider: str, model: str, direction: str) -> float:
    return REGISTRY.get_sample_value("rahbar_llm_tokens_total", {
        "provider": provider, "model": model, "direction": direction}) or 0.0


def missing(provider: str, model: str) -> float:
    return REGISTRY.get_sample_value("rahbar_llm_usage_missing_total", {
        "provider": provider, "model": model}) or 0.0


def answered_fields(caplog: pytest.LogCaptureFixture) -> dict:
    records = [r for r in caplog.records if r.name == "rahbar.answer" and r.msg == "answered"]
    assert records, "no 'answered' log line"
    return vars(records[-1])


def ollama(model: str) -> OllamaProvider:
    return OllamaProvider(Settings(llm_provider="ollama-host", llm_model=model,
                                   ollama_url="http://ollama:11434"))


def ask(provider: Any, cache: Any = None) -> Any:
    return answer_question(QUESTION, FixedRetriever([P]), provider,
                           Settings(relevance_floor=0.45), cache=cache,
                           embedder=HashEmbedder())


def test_ollama_usage_reaches_metrics_and_the_log(monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    fake_post(monkeypatch, module, [ollama_reply(VALID, 812, 47)])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    r = ask(ollama("m-success"))
    assert r.answered_from_context is True
    assert tokens("ollama-host", "m-success", "input") == 812
    assert tokens("ollama-host", "m-success", "output") == 47
    f = answered_fields(caplog)
    assert (f["llm_calls"], f["tokens_in"], f["tokens_out"], f["usage_complete"]) == (1, 812, 47, True)


def test_a_retry_counts_both_calls(monkeypatch: pytest.MonkeyPatch,
                                   caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    calls = fake_post(monkeypatch, module, [ollama_reply("not json", 800, 12),
                                            ollama_reply(VALID, 830, 45)])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    r = ask(ollama("m-retry"))
    assert len(calls) == 2 and r.answered_from_context is True
    assert tokens("ollama-host", "m-retry", "input") == 1630
    assert tokens("ollama-host", "m-retry", "output") == 57
    f = answered_fields(caplog)
    assert (f["llm_calls"], f["tokens_in"], f["tokens_out"]) == (2, 1630, 57)


def test_fallback_still_counts_the_tokens_spent(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    fake_post(monkeypatch, module, [ollama_reply("prose", 700, 30),
                                    ollama_reply("more prose", 720, 31)])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    r = ask(ollama("m-fallback"))
    assert r.answered_from_context is False            # the safe fallback answer
    f = answered_fields(caplog)
    assert (f["llm_calls"], f["tokens_in"], f["tokens_out"]) == (2, 1420, 61)
    assert f["declined"] == "GenerationFailed"


def test_missing_usage_is_unavailable_not_zero(monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import hosted as module

    fake_post(monkeypatch, module, [{"choices": [{"message": {"content": VALID}}]}])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    provider = HostedProvider(Settings(llm_provider="hosted", llm_model="m-nousage",
                                       hosted_base_url="https://llm.example/v1",
                                       llm_api_key="test-key-not-real"))
    before = missing("hosted", "m-nousage")
    ask(provider)
    f = answered_fields(caplog)
    assert f["llm_calls"] == 1
    assert f["tokens_in"] is None and f["tokens_out"] is None
    assert f["usage_complete"] is False
    assert missing("hosted", "m-nousage") == before + 1
    assert tokens("hosted", "m-nousage", "input") == 0      # nothing invented


def test_hosted_usage_is_read_from_the_usage_object(monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import hosted as module

    fake_post(monkeypatch, module, [{"choices": [{"message": {"content": VALID}}],
                                     "usage": {"prompt_tokens": 950, "completion_tokens": 60}}])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    ask(HostedProvider(Settings(llm_provider="hosted", llm_model="m-hosted",
                                hosted_base_url="https://llm.example/v1",
                                llm_api_key="test-key-not-real")))
    assert tokens("hosted", "m-hosted", "input") == 950
    assert tokens("hosted", "m-hosted", "output") == 60
    assert answered_fields(caplog)["tokens_in"] == 950


def test_partial_and_zero_counts_are_kept_apart(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    # Ollama omits prompt_eval_count when it reuses a cached prompt; eval_count is 0 here.
    fake_post(monkeypatch, module, [ollama_reply(VALID, None, 0)])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    ask(ollama("m-partial"))
    f = answered_fields(caplog)
    assert f["tokens_in"] is None          # not reported
    assert f["tokens_out"] == 0            # reported as zero
    assert f["usage_complete"] is False


def test_a_cache_hit_calls_no_model_and_reports_zero(monkeypatch: pytest.MonkeyPatch,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    calls = fake_post(monkeypatch, module, [ollama_reply(VALID, 812, 47)])
    cache = MemoryCache(threshold=0.95, ttl=60, max_entries=10)
    provider = ollama("m-cache")
    ask(provider, cache)
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    caplog.clear()
    second = ask(provider, cache)
    assert second.cached is True and len(calls) == 1
    f = answered_fields(caplog)
    assert (f["cache"], f["llm_calls"], f["tokens_in"], f["tokens_out"]) == ("hit", 0, 0, 0)
    assert tokens("ollama-host", "m-cache", "input") == 812     # only the first request


def test_usage_does_not_leak_between_requests(monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.llm import ollama as module

    fake_post(monkeypatch, module, [ollama_reply(VALID, 100, 10)])
    caplog.set_level(logging.INFO, logger="rahbar.answer")
    provider = ollama("m-isolation")
    ask(provider)
    ask(provider)
    f = answered_fields(caplog)
    assert (f["llm_calls"], f["tokens_in"]) == (1, 100)     # the second request alone
