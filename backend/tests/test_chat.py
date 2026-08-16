"""LLM chat: mock replies, auto-execution rules, history windowing and the HTTP surface
(PLAN.md §9). No test here reaches OpenRouter — `complete()` is stubbed or mocked."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app import chat, db, portfolio, routes
from app.chat import actions, llm, prompt, service
from app.chat.models import AssistantReply, TradeIntent, WatchlistIntent
from app.market import router as market_router
from app.schema import STARTING_CASH


@pytest.fixture(autouse=True)
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with the mock LLM unless it explicitly says otherwise."""
    monkeypatch.setenv("LLM_MOCK", "true")


@pytest_asyncio.fixture
async def client(temp_db, priced_service):
    app = FastAPI()
    app.include_router(market_router)
    app.include_router(routes.router)
    app.include_router(chat.router)
    app.state.market = priced_service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _messages(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


# ---- config (PLAN.md §5, §13 item 7) -----------------------------------------

def test_verify_config_fails_fast_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        llm.verify_config()


def test_verify_config_accepts_mock_mode_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    llm.verify_config()          # LLM_MOCK=true via the autouse fixture


def test_verify_config_accepts_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    llm.verify_config()


# ---- mock replies ------------------------------------------------------------

def test_mock_parses_a_trade_instruction() -> None:
    reply = llm.mock_reply(_messages("please buy 5 MU for me"))
    assert [(t.side, t.quantity, t.ticker) for t in reply.trades] == [("buy", 5.0, "MU")]
    assert "MU" in reply.message


def test_mock_parses_ticker_before_quantity_and_fractions() -> None:
    reply = llm.mock_reply(_messages("sell amd 2.5"))
    assert [(t.side, t.quantity, t.ticker) for t in reply.trades] == [("sell", 2.5, "AMD")]


def test_mock_parses_watchlist_instructions() -> None:
    reply = llm.mock_reply(_messages("watch PYPL and unwatch SLV"))
    assert [(c.action, c.ticker) for c in reply.watchlist_changes] == [
        ("add", "PYPL"), ("remove", "SLV")
    ]


def test_mock_is_deterministic_and_actionless_by_default() -> None:
    first = llm.mock_reply(_messages("how is my portfolio doing?"))
    second = llm.mock_reply(_messages("how is my portfolio doing?"))
    assert first == second
    assert first.trades == [] and first.watchlist_changes == []


@pytest.mark.asyncio
async def test_complete_uses_the_mock_without_touching_litellm() -> None:
    reply = await llm.complete(_messages("buy 1 MU"))
    assert reply.trades[0].ticker == "MU"


# ---- malformed provider responses (PLAN.md §12) ------------------------------

@pytest.mark.asyncio
async def test_malformed_json_becomes_an_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setattr(llm, "_blocking_completion", lambda messages: "not json at all")
    with pytest.raises(llm.LLMError, match="malformed JSON"):
        await llm.complete(_messages("hi"))


@pytest.mark.asyncio
async def test_schema_violation_becomes_an_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    # Valid JSON, wrong shape: `message` missing and `side` is not buy/sell.
    monkeypatch.setattr(
        llm, "_blocking_completion",
        lambda messages: json.dumps({"trades": [{"ticker": "MU", "side": "hodl", "quantity": 1}]}),
    )
    with pytest.raises(llm.LLMError, match="malformed JSON"):
        await llm.complete(_messages("hi"))


@pytest.mark.asyncio
async def test_empty_response_becomes_an_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setattr(llm, "_blocking_completion", lambda messages: "   ")
    with pytest.raises(llm.LLMError, match="empty"):
        await llm.complete(_messages("hi"))


@pytest.mark.asyncio
async def test_provider_failure_becomes_an_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MOCK", "false")

    def boom(messages):
        raise ConnectionError("openrouter unreachable")

    monkeypatch.setattr(llm, "_blocking_completion", boom)
    with pytest.raises(llm.LLMError, match="could not be reached"):
        await llm.complete(_messages("hi"))


# ---- auto-execution (PLAN.md §9, §13 items 5 and 6) --------------------------

@pytest.mark.asyncio
async def test_trades_execute_sequentially_against_the_running_balance(
    temp_db, priced_service
) -> None:
    """Each trade is validated against what its predecessors left, not a shared snapshot."""
    reply = AssistantReply(
        message="buying",
        trades=[TradeIntent(ticker="MU", side="buy", quantity=50),      # $5,000
                TradeIntent(ticker="AMD", side="buy", quantity=80)],    # $4,000
    )
    records = await actions.execute(reply, priced_service)

    assert [r.status for r in records] == ["executed", "executed"]
    state = await db.run(lambda conn: portfolio.value_portfolio(conn, priced_service))
    assert state["cash_balance"] == pytest.approx(STARTING_CASH - 9_000.0)


@pytest.mark.asyncio
async def test_a_rejected_trade_halts_the_rest_of_the_batch(
    temp_db, priced_service
) -> None:
    """Earlier trades stand; the rejection and everything after it report a reason.

    A batch is a plan — "sell X then buy Y with the proceeds" has an unfunded second leg the
    moment the first fails, so continuing would turn a failed plan into an unrequested
    position (PLAN.md §9).
    """
    reply = AssistantReply(
        message="buying a lot",
        trades=[TradeIntent(ticker="MU", side="buy", quantity=90),      # $9,000, fits
                TradeIntent(ticker="AMD", side="buy", quantity=80),     # $4,000, does not
                TradeIntent(ticker="SLV", side="buy", quantity=1)],     # never attempted
    )
    records = await actions.execute(reply, priced_service)

    assert [r.status for r in records] == ["executed", "rejected", "skipped"]
    assert records[1].code == "insufficient_cash"
    assert records[2].code == "batch_halted"

    # The skipped trade really did not execute.
    held = await db.run(lambda conn: db.position(conn, "SLV"))
    assert held is None


@pytest.mark.asyncio
async def test_selling_more_than_held_is_rejected(temp_db, priced_service) -> None:
    reply = AssistantReply(
        message="selling", trades=[TradeIntent(ticker="MU", side="sell", quantity=5)]
    )
    records = await actions.execute(reply, priced_service)
    assert records[0].status == "rejected"
    assert records[0].code == "insufficient_shares"


@pytest.mark.asyncio
async def test_trading_an_unwatched_ticker_adds_it_to_the_watchlist(
    temp_db, priced_service
) -> None:
    """PLAN.md §13 item 6: without this the trade would have no price to fill at."""
    watched = {row["ticker"] for row in await db.run(db.watchlist)}
    assert "PYPL" not in watched

    reply = AssistantReply(
        message="buying", trades=[TradeIntent(ticker="PYPL", side="buy", quantity=1)]
    )
    records = await actions.execute(reply, priced_service)

    assert records[0].status == "executed"
    watched = {row["ticker"] for row in await db.run(db.watchlist)}
    assert "PYPL" in watched
    assert "PYPL" in await db.run(db.tracked_tickers)


@pytest.mark.asyncio
async def test_watchlist_changes_execute(temp_db, priced_service) -> None:
    reply = AssistantReply(
        message="curating",
        watchlist_changes=[WatchlistIntent(ticker="NVDA", action="add"),
                           WatchlistIntent(ticker="SLV", action="remove")],
    )
    records = await actions.execute(reply, priced_service)

    assert [r.status for r in records] == ["executed", "executed"]
    watched = {row["ticker"] for row in await db.run(db.watchlist)}
    assert "NVDA" in watched and "SLV" not in watched


@pytest.mark.asyncio
async def test_a_malformed_watchlist_symbol_is_reported_not_fatal(
    temp_db, priced_service
) -> None:
    reply = AssistantReply(
        message="curating",
        watchlist_changes=[WatchlistIntent(ticker="not ok", action="add"),
                           WatchlistIntent(ticker="NVDA", action="add")],
    )
    records = await actions.execute(reply, priced_service)
    assert records[0].status == "rejected" and records[0].code == "invalid_ticker"
    assert records[1].status == "executed"          # the batch carried on


@pytest.mark.asyncio
async def test_removing_an_unwatched_ticker_is_skipped_not_failed(
    temp_db, priced_service
) -> None:
    reply = AssistantReply(
        message="curating",
        watchlist_changes=[WatchlistIntent(ticker="NVDA", action="remove")],
    )
    records = await actions.execute(reply, priced_service)
    assert records[0].status == "skipped"


@pytest.mark.asyncio
async def test_a_position_keeps_ticking_after_its_ticker_leaves_the_watchlist(
    temp_db, priced_service
) -> None:
    """The tracked set is the UNION of watchlist and open positions (PLAN.md §13 item 4)."""
    await actions.execute(
        AssistantReply(message="buy", trades=[TradeIntent(ticker="MU", side="buy", quantity=1)]),
        priced_service,
    )
    await actions.execute(
        AssistantReply(
            message="drop it",
            watchlist_changes=[WatchlistIntent(ticker="MU", action="remove")],
        ),
        priced_service,
    )
    assert "MU" in await db.run(db.tracked_tickers)
    assert "MU" in priced_service._tracked


# ---- history windowing (PLAN.md §9 step 2, §13 item 8) -----------------------

@pytest.mark.asyncio
async def test_history_returns_the_most_recent_capped_oldest_first(temp_db) -> None:
    def seed(conn):
        for i in range(60):
            db.add_chat_message(conn, "user", f"message {i:02d}")
        conn.commit()

    await db.run(seed)
    rows = await db.run(lambda conn: db.chat_messages(conn, limit=50))

    assert len(rows) == 50
    # The 50 MOST RECENT, not the first 50 — and ascending for the prompt.
    assert rows[0]["content"] == "message 10"
    assert rows[-1]["content"] == "message 59"


@pytest.mark.asyncio
async def test_history_excludes_messages_older_than_the_window(temp_db) -> None:
    def seed(conn):
        conn.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES ('old', 'default', 'user', 'ancient', NULL, '2000-01-01T00:00:00Z')"
        )
        db.add_chat_message(conn, "user", "recent")
        conn.commit()

    await db.run(seed)
    within = await db.run(lambda conn: db.chat_messages(conn, days=30))
    assert [r["content"] for r in within] == ["recent"]

    # days=None lifts the filter, which is what the UI restore uses.
    everything = await db.run(lambda conn: db.chat_messages(conn, days=None))
    assert [r["content"] for r in everything] == ["ancient", "recent"]


# ---- the full turn -----------------------------------------------------------

@pytest.mark.asyncio
async def test_respond_persists_both_turns_with_actions(temp_db, priced_service) -> None:
    result = await service.respond("buy 5 MU", priced_service)

    assert result["message"]["role"] == "assistant"
    assert result["message"]["actions"][0]["status"] == "executed"
    assert result["portfolio"]["cash_balance"] == pytest.approx(STARTING_CASH - 500.0)

    stored = await db.run(lambda conn: db.chat_messages(conn, days=None))
    assert [row["role"] for row in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "buy 5 MU"
    assert json.loads(stored[1]["actions"])[0]["ticker"] == "MU"


@pytest.mark.asyncio
async def test_a_failed_turn_persists_nothing(
    temp_db, priced_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dangling user row with no answer would replay into every later prompt as a
    question the assistant appears to have ignored."""
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setattr(llm, "_blocking_completion", lambda messages: "garbage")

    with pytest.raises(llm.LLMError):
        await service.respond("hello", priced_service)

    assert await db.run(lambda conn: db.chat_messages(conn, days=None)) == []


@pytest.mark.asyncio
async def test_respond_omits_the_portfolio_when_nothing_executed(
    temp_db, priced_service
) -> None:
    result = await service.respond("how am I doing?", priced_service)
    assert result["portfolio"] is None
    assert result["message"]["actions"] == []


@pytest.mark.asyncio
async def test_respond_rejects_an_empty_message(temp_db, priced_service) -> None:
    with pytest.raises(ValueError):
        await service.respond("   ", priced_service)


# ---- prompt construction -----------------------------------------------------

def test_context_reports_the_mode_honestly() -> None:
    state = {
        "cash_balance": 10000.0, "positions": [], "positions_value": 0.0,
        "total_value": 10000.0, "starting_cash": 10000.0, "total_return": 0.0,
        "total_return_pct": 0.0,
    }
    rendered = prompt.render_context(state, [], "anchored")
    assert "ANCHORED" in rendered
    assert "simulated" in rendered.lower()      # tells the model not to invent news
    assert "(none — the portfolio is entirely cash)" in rendered


def test_build_messages_orders_system_context_history_then_user() -> None:
    messages = prompt.build_messages(
        "CTX", [{"role": "user", "content": "earlier"}], "now"
    )
    assert [m["role"] for m in messages] == ["system", "system", "user", "user"]
    assert "CTX" in messages[1]["content"]
    assert messages[-1]["content"] == "now"


# ---- HTTP surface (PLAN.md §8) -----------------------------------------------

@pytest.mark.asyncio
async def test_post_chat_returns_reply_and_actions(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"message": "buy 3 MU"})
    assert response.status_code == 200
    body = response.json()
    assert body["message"]["role"] == "assistant"
    assert body["message"]["actions"][0]["ticker"] == "MU"
    assert body["portfolio"]["cash_balance"] == pytest.approx(STARTING_CASH - 300.0)


@pytest.mark.asyncio
async def test_post_chat_rejects_an_empty_message(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/chat", json={"message": ""})).status_code == 422


@pytest.mark.asyncio
async def test_post_chat_maps_provider_failure_to_502(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """502 not 500: the failure is upstream, and the panel shows it as retryable."""
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setattr(llm, "_blocking_completion", lambda messages: "garbage")
    response = await client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_chat_history_round_trips_through_the_api(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/api/chat", json={"message": "buy 1 MU"})
    body = (await client.get("/api/chat/history")).json()

    assert body["mock"] is True
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    # Actions survive the round trip, so a reload shows the same inline confirmations.
    assert body["messages"][1]["actions"][0]["status"] == "executed"


@pytest.mark.asyncio
async def test_reset_clears_the_conversation(client: httpx.AsyncClient) -> None:
    await client.post("/api/chat", json={"message": "buy 1 MU"})
    await client.post("/api/portfolio/reset")
    assert (await client.get("/api/chat/history")).json()["messages"] == []
