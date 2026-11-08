"""Liveness and readiness (section 4.3). They answer different questions.

/healthz: is this process alive? It touches nothing, so a slow Chroma can never make
Kubernetes restart healthy API pods.
/readyz: can this pod answer questions right now? Chroma reachable, collection present
and non-empty, its embedding model equal to EMBED_MODEL, Redis reachable when Redis is
the cache. Any failure is a 503 that names the failed dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter()
log = logging.getLogger("rahbar.health")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


def run_checks(state: Any) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    checks_of = getattr(state.retriever, "readiness_checks", None)
    if callable(checks_of):
        checks.update(checks_of())
    else:
        checks["retriever"] = {"ok": True, "note": f"{state.retriever.name}: nothing to check"}
    cache = state.cache
    if cache.name == "redis":
        try:
            checks["redis"] = {"ok": bool(cache.ping())}
        except Exception as exc:
            checks["redis"] = {"ok": False, "error": f"redis unreachable: {exc}"}
    return checks


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    checks = run_checks(request.app.state)
    failed = [name for name, result in checks.items() if not result.get("ok")]
    if len(failed) > 0:
        response.status_code = 503
        log.warning("not ready", extra={"failed": failed,
                                        "errors": {n: checks[n].get("error") for n in failed}})
        return {"status": "not ready", "failed": failed, "checks": checks}
    return {"status": "ready", "checks": checks}
