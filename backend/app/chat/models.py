"""The structured-output contract (PLAN.md §9) and the action records it produces.

The schema doubles as the LLM's response format and the parser for it: LiteLLM turns
`AssistantReply` into a JSON schema on the way out and we validate the model's JSON back
into it on the way in, so a malformed response fails here rather than three layers deeper.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MAX_TRADES_PER_REPLY = 10
MAX_WATCHLIST_CHANGES_PER_REPLY = 10


class TradeIntent(BaseModel):
    """One trade the LLM wants executed. Validated again by `portfolio.execute_trade` —
    this only checks the shape, never the affordability."""

    ticker: str = Field(..., min_length=1, max_length=12)
    side: Literal["buy", "sell"]
    quantity: float


class WatchlistIntent(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    action: Literal["add", "remove"]


class AssistantReply(BaseModel):
    """Exactly the JSON shape PLAN.md §9 specifies.

    `trades` and `watchlist_changes` default to empty rather than being nullable: a reply
    that is pure conversation is the common case, and a default spares every caller a
    `or []`. The caps are a blast radius, not a style rule — a looping model asking for 400
    trades would otherwise execute all 400 against real balances before anyone saw the
    reply.
    """

    message: str = Field(..., description="Conversational response shown to the user")
    trades: list[TradeIntent] = Field(
        default_factory=list, max_length=MAX_TRADES_PER_REPLY
    )
    watchlist_changes: list[WatchlistIntent] = Field(
        default_factory=list, max_length=MAX_WATCHLIST_CHANGES_PER_REPLY
    )


class ActionRecord(BaseModel):
    """What actually happened, which is not always what the LLM asked for.

    Persisted as the `actions` JSON column and replayed to the frontend on history load, so
    a reload shows the same inline confirmations the live response did.
    """

    kind: Literal["trade", "watchlist"]
    status: Literal["executed", "rejected", "skipped"]
    ticker: str
    detail: str
    side: str | None = None
    quantity: float | None = None
    fill_price: float | None = None
    total: float | None = None
    action: str | None = None
    code: str | None = None
