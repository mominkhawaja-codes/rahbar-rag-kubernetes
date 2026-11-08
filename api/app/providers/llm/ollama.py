"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

Constrained decoding: the Answer JSON schema is sent as Ollama's ``format`` parameter,
so the model can only emit JSON of that shape. It is still validated (prompt.py),
because a schema-shaped reply can still be wrong (an empty answer, a bad enum).
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .prompt import answer_schema, generate_with_retry
from .usage import record_usage


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings   # ollama_url, llm_model, temperature
        self.name = settings.llm_provider          # "ollama-host" or "ollama-incluster"
        self.url = f"{settings.ollama_url.rstrip('/')}/api/chat"

    def _call(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "format": answer_schema(),
            "options": {"temperature": self.settings.temperature},
        }
        response = httpx.post(self.url, json=payload, timeout=self.settings.llm_timeout)
        response.raise_for_status()
        body = response.json()
        record_usage(self.name, self.settings.llm_model,
                     body.get("prompt_eval_count"), body.get("eval_count"))
        return str(body["message"]["content"])

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        return generate_with_retry(self._call, question, passages)
