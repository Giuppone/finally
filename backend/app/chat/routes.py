"""Chat HTTP surface (PLAN.md §8).

No token streaming: Cerebras inference is fast enough that a loading indicator is
sufficient, and one complete JSON response keeps auto-execution atomic with the reply that
describes it (PLAN.md §9).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..market import MarketDataService, get_service
from . import llm, service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=service.MAX_MESSAGE_CHARS)


@router.post("")
async def post_chat(
    body: ChatRequest,
    market: MarketDataService = Depends(get_service),
) -> dict:
    try:
        return await service.respond(body.message, market)
    except llm.LLMError as exc:
        # 502, not 500: the failure is upstream at the model provider, and the frontend
        # shows it as a retryable chat error rather than a broken app.
        log.warning("chat failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/history")
async def get_history(
    limit: int = Query(service.UI_HISTORY_LIMIT, ge=1, le=1_000),
) -> dict:
    """Restores the panel on mount, so a refresh does not show an empty conversation the
    assistant still has context for (PLAN.md §13 item 4)."""
    return {"messages": await service.history(limit=limit), "mock": llm.mock_enabled()}
