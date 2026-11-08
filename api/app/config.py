"""Runtime configuration, read once from environment variables.

On the cluster these variables come from the ConfigMap and Secret (section 4.1).
Nothing here is baked into the image, so one image runs in every environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Which implementations to load. "stub" needs no cluster, no model, no network.
    retriever: str = "stub"          # RETRIEVER: stub | chroma
    llm_provider: str = "stub"       # LLM_PROVIDER: stub | ollama-host | ollama-incluster | hosted

    # The embedding pin (section 4.4). The indexer reads the same ConfigMap key.
    embed_model: str = "nomic-embed-text"

    # Vector store
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    collection: str = "rahbar"

    # Retrieval and generation
    top_k: int = 5
    temperature: float = 0.2
    llm_model: str = "llama3.2:3b"
    ollama_url: str = "http://host.minikube.internal:11434"
    hosted_base_url: str = ""        # e.g. an OpenAI-compatible endpoint
    hosted_json_mode: str = "json_object"   # HOSTED_JSON_MODE: json_object | json_schema
    llm_api_key: str = field(default="", repr=False)   # from the Secret; never log it
    llm_timeout: float = 60.0        # LLM_TIMEOUT seconds, per attempt
    embed_timeout: float = 20.0      # EMBED_TIMEOUT seconds

    # Grounding (section 3.4): passages below the floor are never shown to the model;
    # when none is left, the API declines without calling the model.
    relevance_floor: float = 0.45    # RELEVANCE_FLOOR, cosine similarity

    # Semantic cache (section 3.3)
    cache_backend: str = "memory"    # CACHE_BACKEND: redis | memory | none
    redis_url: str = "redis://redis:6379/0"
    cache_threshold: float = 0.95    # CACHE_THRESHOLD, cosine similarity of the questions
    cache_ttl: int = 86400           # CACHE_TTL seconds
    cache_max_entries: int = 2000    # CACHE_MAX_ENTRIES per index generation

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings(
        retriever=os.environ.get("RETRIEVER", "stub"),
        llm_provider=os.environ.get("LLM_PROVIDER", "stub"),
        embed_model=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        chroma_host=os.environ.get("CHROMA_HOST", "chroma"),
        chroma_port=_int("CHROMA_PORT", 8000),
        collection=os.environ.get("CHROMA_COLLECTION", "rahbar"),
        top_k=_int("TOP_K", 5),
        temperature=_float("TEMPERATURE", 0.2),
        llm_model=os.environ.get("LLM_MODEL", "llama3.2:3b"),
        ollama_url=os.environ.get("OLLAMA_URL", "http://host.minikube.internal:11434"),
        hosted_base_url=os.environ.get("HOSTED_BASE_URL", ""),
        hosted_json_mode=os.environ.get("HOSTED_JSON_MODE", "json_object"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_timeout=_float("LLM_TIMEOUT", 60.0),
        embed_timeout=_float("EMBED_TIMEOUT", 20.0),
        relevance_floor=_float("RELEVANCE_FLOOR", 0.45),
        cache_backend=os.environ.get("CACHE_BACKEND", "memory"),
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        cache_threshold=_float("CACHE_THRESHOLD", 0.95),
        cache_ttl=_int("CACHE_TTL", 86400),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 2000),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
