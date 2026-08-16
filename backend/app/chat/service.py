"""Chat orchestration — the eight steps of PLAN.md §9, in order.

Load context -> load history -> build the prompt -> call the model -> parse -> auto-execute
-> persist -> return.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref

from .. import db, portfolio
from ..market import MarketDataService
from ..schema import DEFAULT_USER
from . import actions as action_runner
from . import llm, prompt
from .models import ActionRecord

log = logging.getLogger(__name__)

HISTORY_DAYS = 30           # PLAN.md §9 step 2 / §13 item 8
HISTORY_LIMIT = 50
UI_HISTORY_LIMIT = 200      # what the panel restores on mount — generous, still bounded
MAX_MESSAGE_CHARS = 4_000

# One chat turn at a time, per event loop. Built per-loop for the reason spelled out in
# `portfolio.trade_lock`: an asyncio.Lock binds to the loop that first acquires it.
#
# Without this, double-clicking Send runs two turns against the same history and the same
# balance. Each individual trade is still safe (`portfolio.trade_lock` sees to that), but
# the user gets the same batch executed twice — two positions where they asked for one.
_CHAT_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def chat_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _CHAT_LOCKS.get(loop)
    if lock is None:
        lock = _CHAT_LOCKS[loop] = asyncio.Lock()
    return lock


def _row_to_message(row) -> dict:
    """One wire message. Same shape from `/api/chat` and `/api/chat/history`, so the panel
    has one renderer and a reload looks identical to the live turn."""
    raw = row["actions"]
    parsed: list = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:            # never fail a history load over one row
            log.warning("chat row %s has unparseable actions JSON", row["id"])
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "actions": parsed,
        "created_at": row["created_at"],
    }


async def history(limit: int = UI_HISTORY_LIMIT, user_id: str = DEFAULT_USER) -> list[dict]:
    """Prior conversation for the panel restore (PLAN.md §13 item 4). No age filter: a
    reload should show what the user last saw, however long ago that was."""
    rows = await db.run(
        lambda conn: db.chat_messages(conn, limit=limit, days=None, user_id=user_id)
    )
    return [_row_to_message(row) for row in rows]


async def respond(
    user_message: str,
    service: MarketDataService,
    user_id: str = DEFAULT_USER,
) -> dict:
    """One full chat turn. Raises `llm.LLMError` if the model cannot be used."""
    text = user_message.strip()
    if not text:
        raise ValueError("message must not be empty")
    text = text[:MAX_MESSAGE_CHARS]

    async with chat_lock():
        # 1 + 2: portfolio context and conversation history.
        state = await db.run(lambda conn: portfolio.value_portfolio(conn, service, user_id))
        watchlist = await _watchlist_with_prices(service, user_id)
        past = await db.run(
            lambda conn: db.chat_messages(
                conn, limit=HISTORY_LIMIT, days=HISTORY_DAYS, user_id=user_id
            )
        )

        # 3 + 4 + 5: prompt, call, parse.
        messages = prompt.build_messages(
            prompt.render_context(state, watchlist, str(service.mode)),
            [{"role": row["role"], "content": row["content"]} for row in past],
            text,
        )
        reply = await llm.complete(messages)      # raises LLMError; nothing persisted yet

        # 6: auto-execute.
        records = await action_runner.execute(reply, service, user_id)

        # 7: persist both turns together, only now that the turn actually succeeded. A
        # failed call leaves no trace: a dangling user row with no answer would replay into
        # every later prompt as a question the assistant appears to have ignored.
        stored = await db.run(
            lambda conn: _persist(conn, text, reply.message, records, user_id)
        )

        # 8: return. The fresh portfolio rides along when state changed, so the header and
        # positions table update without waiting for the next poll.
        changed = any(r.status == "executed" for r in records)
        fresh = (
            await db.run(lambda conn: portfolio.value_portfolio(conn, service, user_id))
            if changed else None
        )

    return {"message": stored, "portfolio": fresh}


def _persist(conn, user_text: str, assistant_text: str,
             records: list[ActionRecord], user_id: str) -> dict:
    payload = json.dumps([r.model_dump(exclude_none=True) for r in records]) if records else None
    with db.transaction(conn):
        db.add_chat_message(conn, "user", user_text, None, user_id)
        row = db.add_chat_message(conn, "assistant", assistant_text, payload, user_id)
    return _row_to_message(row)


async def _watchlist_with_prices(service: MarketDataService, user_id: str) -> list[dict]:
    rows = await db.run(lambda conn: db.watchlist(conn, user_id))
    entries = []
    for row in rows:
        quote = service.quote(row["ticker"])
        entry = {"ticker": row["ticker"], "priced": quote is not None}
        entries.append(entry | (quote.to_wire() if quote else {}))
    return entries
