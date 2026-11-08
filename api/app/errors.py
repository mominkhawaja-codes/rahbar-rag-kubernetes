"""Errors that mean "a dependency is down", which the API answers with 503, not 500."""

from __future__ import annotations


class DependencyUnavailable(RuntimeError):
    """Chroma, the collection, or the embedding model cannot serve this request."""

    dependency = "dependency"
