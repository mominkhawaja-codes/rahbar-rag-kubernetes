"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

Constrained decoding: the Answer JSON schema is sent as Ollama's ``format`` parameter,
so the model can only emit JSON of that shape. It is still validated (prompt.py),
because a schema-shaped reply can still be wrong (an empty answer, a bad enum).
"""

from __future__ import annotations

import logging
import time

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .prompt import answer_schema, generate_with_retry
from .usage import record_usage

log = logging.getLogger("rahbar.llm.ollama")


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings   # ollama_url, llm_model, temperature
        self.name = settings.llm_provider          # "ollama-host" or "ollama-incluster"
        self.url = f"{settings.ollama_url.rstrip('/')}/api/chat"

    def _call(self, messages: list[dict[str, str]], timeout: float | None = None) -> str:
        options = {"temperature": self.settings.temperature}
        if self.settings.ollama_num_threads > 0:
            options["num_thread"] = self.settings.ollama_num_threads
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "format": answer_schema(),
            "options": options,
        }
        response = httpx.post(self.url, json=payload,
                              timeout=self.settings.llm_timeout if timeout is None else timeout)
        response.raise_for_status()
        body = response.json()
        record_usage(self.name, self.settings.llm_model,
                     body.get("prompt_eval_count"), body.get("eval_count"))
        log.info("ollama timings", extra={
            field: body.get(field) for field in (
                "load_duration", "prompt_eval_count", "prompt_eval_duration",
                "eval_count", "eval_duration", "total_duration")
        })
        return str(body["message"]["content"])

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        deadline = time.monotonic() + self.settings.llm_timeout

        def call(messages: list[dict[str, str]]) -> str:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.ReadTimeout("Ollama generation budget exhausted")
            return self._call(messages, timeout=remaining)

        # The format parameter already supplies the schema to constrained decoding.
        return generate_with_retry(call, question, passages, include_schema=False)
