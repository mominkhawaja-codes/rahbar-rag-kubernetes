# ADR 0004: LLM provider: Ollama by default, a hosted API behind one interface, no PII sent

## Status
Accepted · 8 Nov 2026

## Context
The system needs a model that can follow a JSON schema, on laptops with 8-16 GB of RAM,
and a fallback when a laptop cannot run one. Questions come from students and may contain
personal details ("my roll number is 22I-1234, my CGPA is 1.9…"), and a hosted provider
would receive whatever we send it.

## Decision
- One interface, `LLMProvider.generate(question, passages) -> Answer`
  (`api/app/providers/llm/base.py`), with four implementations chosen by `LLM_PROVIDER`:
  `stub` (tests and CI; deterministic, no network), `ollama-host` and `ollama-incluster`
  (`ollama.py`), and `hosted` for any OpenAI-compatible endpoint, e.g. Groq (`hosted.py`).
- **Default for dev: Ollama on the host** with `llama3.2:3b`. Ollama's `format` parameter
  takes the `Answer` JSON schema, so decoding is constrained to the contract.
- **Hosted** sends `response_format` (`json_object`, or `json_schema` with
  `HOSTED_JSON_MODE=json_schema`). The key comes only from the `rahbar-secret` Secret,
  created with `make secret` from an environment variable. It never appears in a file, in
  `Settings.__repr__`, or in an error message (`HostedProviderError` carries only the
  status code; there is a test).
- Both real providers share `prompt.py`: the same grounded prompt, validation with
  Pydantic, one retry with the validation error shown to the model, then
  `GenerationFailed`. The service turns that, a timeout (`LLM_TIMEOUT`) or an HTTP error
  into a safe fallback answer, never a 500.
- **PII:**
  - What leaves the cluster for a hosted provider is the system prompt, the retrieved
    passages (public university documents) and the question.
  - Questions are not stored anywhere except the semantic cache: in-cluster, 24 h TTL,
    never logged in full (the `answered` log line holds chunk ids, scores and the outcome,
    not the question).
  - With a hosted provider, the question itself still goes to that provider. Students must
    not type personal details, and the UI should say so. Ollama keeps everything on the
    host.

## Options considered
- **Only a hosted model:** best answers and nothing heavy on the laptop, but a free tier
  rate-limits a class of forty teams, and every question leaves the university's machines.
- **Only Ollama:** private and free, but a 3B model on a laptop is slow, and some
  machines cannot run it.
- **Redacting PII before sending:** worth adding (roll numbers, CNICs, phone numbers have
  regular shapes), not built yet. It is the first follow-up if a hosted provider is used
  with real students.

## Consequences
- Easier: swapping providers is one ConfigMap value. CI never depends on a model, because
  it uses the stub for generation (embeddings come from an in-cluster Ollama).
- Harder: answers differ in quality between providers, so evaluation numbers must always
  name the provider used (`results.json` records it per response).
- Watch: `rahbar_fallbacks_total` by reason and `rahbar_generation_seconds`. A rising
  fallback rate after a model change means the model follows the schema worse.
