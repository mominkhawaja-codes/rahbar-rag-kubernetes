"""POST /api/ask. The Ingress sends /api/* here unchanged, so the prefix stays."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ..schemas import AskRequest, AskResponse
from ..services.answer import answer_question

router = APIRouter(prefix="/api")


@router.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request, response: Response) -> AskResponse:
    state = request.app.state
    result = answer_question(body.question, state.retriever, state.provider, state.settings,
                             cache=state.cache, embedder=state.embedder)
    response.headers["X-Cache"] = "hit" if result.cached else "miss"
    return result
