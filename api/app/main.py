"""The API process. It reads the index; it never builds it (section 4.2).

Run locally with stubs:   make dev     (then open http://localhost:8000)
Run in the container:     uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .embeddings import get_embedder
from .errors import DependencyUnavailable
from .logjson import configure_logging, request_id_var
from .providers.llm import get_provider
from .retrieval import get_retriever
from .routes import ask, health, metrics
from .routes.health import run_checks
from .services.cache import get_cache

log = logging.getLogger("rahbar.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    app.state.embedder = get_embedder(settings)
    app.state.retriever = get_retriever(settings, embedder=app.state.embedder)
    app.state.provider = get_provider(settings)
    app.state.cache = get_cache(settings)
    # The embedding pin (4.4): log a mismatch loudly at start-up. /readyz repeats the
    # check on every probe and keeps the pod out of the Service until it is fixed.
    # This process only reads the index; the indexer Job writes it.
    pin = run_checks(app.state).get("embed_pin")
    if pin is not None and not pin.get("ok"):
        log.error("embedding pin check failed", extra={"error": pin.get("error")})
    log.info(
        "api started",
        extra={"retriever": settings.retriever, "llm_provider": settings.llm_provider,
               "embed_model": settings.embed_model, "top_k": settings.top_k,
               "cache": app.state.cache.name, "relevance_floor": settings.relevance_floor},
    )
    yield


async def dependency_unavailable(request: Request, exc: Exception) -> JSONResponse:
    dep = getattr(exc, "dependency", "dependency")
    log.error("dependency unavailable", extra={"dependency": dep, "error": str(exc)[:300]})
    return JSONResponse(status_code=503, content={
        "detail": f"{dep} unavailable; please try again shortly",
        "request_id": request_id_var.get()})


async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={
        "detail": "internal error", "request_id": request_id_var.get()})


app = FastAPI(title="RAHBAR API", lifespan=lifespan)
app.add_exception_handler(DependencyUnavailable, dependency_unavailable)
app.add_exception_handler(Exception, unhandled)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")   # route template, not the raw URL
        elapsed = time.perf_counter() - start
        metrics.REQUESTS.labels(path=path, status=str(status)).inc()
        metrics.LATENCY.labels(path=path).observe(elapsed)
        if path not in ("/healthz", "/readyz", "/metrics"):
            log.info("request", extra={"path": path, "status": status,
                                       "ms": round(elapsed * 1000, 1)})
        request_id_var.reset(token)


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(ask.router)

# Development only: `make dev` serves the chat UI from this process, so you can try it
# without a cluster. On Kubernetes the ui Deployment serves it and this stays unset.
_ui_dir = os.environ.get("SERVE_UI_DIR")
if _ui_dir:
    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="ui")
