"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

router = APIRouter(prefix="/api")


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest, request: Request, response: Response) -> AskResponse:
    state = request.app.state
    # Reject excess work before it queues behind long, blocking model calls.
    if state.ask_slots.locked():
        raise HTTPException(status_code=429, detail="API busy; try again shortly",
                            headers={"Retry-After": "5"})
    async with state.ask_slots:
        result = await run_in_threadpool(
            answer_question, body.question, state.retriever, state.provider, state.settings,
            cache=state.cache, embedder=state.embedder)
    response.headers["X-Cache"] = "hit" if result.cached else "miss"
    return result
