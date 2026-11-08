"""Token usage per LLM call, as the provider reports it (section 4.8).

Every real provider calls `record_usage` once per HTTP attempt, so a retry is counted as
the second call it is. Values come only from the provider's reply (Ollama:
`prompt_eval_count`/`eval_count`; OpenAI-compatible: `usage.prompt_tokens`/
`usage.completion_tokens`); a value the provider did not send is recorded as *unavailable*,
never as 0.

The per-request total lives in a ContextVar, not on the provider object: one provider
instance serves concurrent requests, and each request runs in its own context.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ...routes import metrics


@dataclass
class TokenUsage:
    """What one request's LLM calls consumed."""

    calls: int = 0
    input_reported: int = 0      # calls whose reply included an input-token count
    output_reported: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def log_fields(self) -> dict[str, Any]:
        """`tokens_in`/`tokens_out` are None when no call reported them, 0 when no model
        was called at all (cache hit, abstention before generation)."""
        return {
            "llm_calls": self.calls,
            "tokens_in": self.input_tokens if self.input_reported or not self.calls else None,
            "tokens_out": self.output_tokens if self.output_reported or not self.calls else None,
            "usage_complete": (self.input_reported == self.calls
                               and self.output_reported == self.calls),
        }


usage_var: ContextVar[TokenUsage | None] = ContextVar("llm_usage", default=None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 \
        else None


def record_usage(provider: str, model: str, input_tokens: Any, output_tokens: Any) -> None:
    """Record one LLM call. Pass the raw values from the reply (None when absent)."""
    tokens_in, tokens_out = _count(input_tokens), _count(output_tokens)
    if tokens_in is not None:
        metrics.LLM_TOKENS.labels(provider=provider, model=model, direction="input").inc(tokens_in)
    if tokens_out is not None:
        metrics.LLM_TOKENS.labels(provider=provider, model=model, direction="output").inc(tokens_out)
    if tokens_in is None or tokens_out is None:
        metrics.LLM_USAGE_MISSING.labels(provider=provider, model=model).inc()
    usage = usage_var.get()
    if usage is None:
        return
    usage.calls += 1
    if tokens_in is not None:
        usage.input_reported += 1
        usage.input_tokens += tokens_in
    if tokens_out is not None:
        usage.output_reported += 1
        usage.output_tokens += tokens_out
