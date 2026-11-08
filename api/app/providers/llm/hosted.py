"""Hosted free-tier provider over an OpenAI-compatible API (Groq, OpenRouter, Google AI
Studio's OpenAI endpoint).

The key arrives as LLM_API_KEY from a Kubernetes Secret created on the command
line. It never appears in a file we commit, and never in a log line: errors are
re-raised with the status code only, not the request.

Structured output: HOSTED_JSON_MODE=json_schema sends the Answer schema as
``response_format`` (strict JSON-schema decoding, where the provider supports it);
json_object (the default) asks for JSON mode and relies on the schema in the prompt.
Either way the reply is validated.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .prompt import answer_schema, generate_with_retry


class HostedProviderError(RuntimeError):
    pass


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        if not settings.hosted_base_url:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL")
        if not settings.llm_api_key:
            raise ValueError("LLM_PROVIDER=hosted needs LLM_API_KEY (from the Secret)")
        self.settings = settings
        self.url = f"{settings.hosted_base_url.rstrip('/')}/chat/completions"

    def _response_format(self) -> dict[str, Any]:
        if self.settings.hosted_json_mode == "json_schema":
            return {"type": "json_schema",
                    "json_schema": {"name": "answer", "schema": answer_schema()}}
        return {"type": "json_object"}

    def _call(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "response_format": self._response_format(),
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        response = httpx.post(self.url, json=payload, headers=headers,
                              timeout=self.settings.llm_timeout)
        if response.status_code >= 400:
            # never include the request (it carries the key) in the error
            raise HostedProviderError(f"hosted LLM returned HTTP {response.status_code}")
        return str(response.json()["choices"][0]["message"]["content"])

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        return generate_with_retry(self._call, question, passages)
