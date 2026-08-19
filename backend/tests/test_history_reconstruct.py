"""The CEDEAR reconstruction: ratios, the calendar, the carry bucket, opening cash.

Synthetic fixtures throughout - no network, no container, and deliberately not the real
`example/compras-ventas-fechas.txt`, which is personal data and will not exist in CI. The one
test that does touch the committed artifact skips when it is absent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.history import bars as bars_module
from app.history import ledger as ledger_module
from app.history import reconstruct, session
from app.history.bars import Bars
from app.history.ledger import Holding, LedgerDocument, LedgerRow

RATE = 1000.0  # a round ARS/USD rate, so every expected figure below is checkable by eye


def make_bars(series: dict[str, dict[str, float]]) -> Bars:
    closes = {
        ticker: {date.fromisoformat(day): close for day, close in points.items()}
        for ticker, points in series.items()
    }
    days = sorted({day for points in closes.values() for day in points})
    return Bars(closes=closes, days=tuple(days), fetched_at="2026-08-18T00:00:00+00:00")


def conversion(day: str) -> list[LedgerRow]:
    """A same-day bond pair, which is the only way FX enters a document."""
    return [
        LedgerRow(day, "buy", "AL30", 100.0, "USD", 1.0, 100.0),
        LedgerRow(day, "sell", "AL30", 100.0, "ARS", RATE, 100.0 * RATE),
    ]


def document(rows: list[LedgerRow], **kwargs) -> LedgerDocument:
    return LedgerDocument(rows=sorted(rows, key=lambda r: (r.date, r.ticker)), **kwargs)


# ---- ratios ----------------------------------------------------------------------

def test_ratio_is_measured_from_the_trade():
    """One CEDEAR at $10 against a $100 share is a 10:1 ratio. No table, no lookup."""
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 50.0, "USD", 10.0, 500.0),
    ]
    bars = make_bars({"AAPL": {"2026-01-05": 100.0}})
    result = reconstruct.build(document(rows), bars)
    assert result.ratios["AAPL"] == pytest.approx(10.0)
    assert result.ratio_sources["AAPL"] == "ledger"
    # 50 CEDEARs at 10:1 is five shares.
    assert result.positions["AAPL"] == pytest.approx(5.0)


def test_peso_priced_trade_uses_the_measured_rate():
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 50.0, "ARS", 10.0 * RATE, 500.0 * RATE),
    ]
    result = reconstruct.build(document(rows), make_bars({"AAPL": {"2026-01-05": 100.0}}))
    assert result.ratios["AAPL"] == pytest.approx(10.0)


def test_ratio_is_the_median_so_one_outlier_cannot_move_it():
    """A single fill at an intraday extreme would drag a mean, and a wrong ratio silently
    rescales that whole position for the entire curve."""
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-06", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-07", "buy", "AAPL", 10.0, "USD", 2.0, 20.0),   # the outlier
    ]
    bars = make_bars({"AAPL": dict.fromkeys(
        ("2026-01-05", "2026-01-06", "2026-01-07"), 100.0)})
    result = reconstruct.build(document(rows), bars)
    assert result.ratios["AAPL"] == pytest.approx(10.0)   # a mean would give ~23


def test_ratio_falls_back_to_the_holdings_snapshot_for_a_never_traded_name():
    """REGRESSION. A ticker held across the whole window but never traded inside it has no
    trade to measure against - GOOGL, on the real file.

    Without this fallback it silently leaves the priced set and its entire value disappears
    from the curve, with no error anywhere. The only symptom is a total that is too small.
    """
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
    ]
    doc = document(
        rows,
        opening={"GOOGL": 200.0},
        snapshot_date="2026-01-06",
        snapshot={"GOOGL": Holding(quantity=200.0, price_ars=5.0 * RATE)},
    )
    bars = make_bars({
        "AAPL": {"2026-01-05": 100.0, "2026-01-06": 100.0},
        "GOOGL": {"2026-01-05": 100.0, "2026-01-06": 100.0},
    })
    result = reconstruct.build(doc, bars)
    assert result.ratio_sources["GOOGL"] == "snapshot"
    assert result.ratios["GOOGL"] == pytest.approx(20.0)     # 100 / (5000/1000)
    assert "GOOGL" in result.priced
    assert result.positions["GOOGL"] == pytest.approx(10.0)


# ---- the calendar ----------------------------------------------------------------

def test_ledger_date_that_is_not_a_trading_day_still_applies():
    """REGRESSION. 2026-01-19 was Martin Luther King Jr Day: the US market was shut, so it is
    not a trading day, but the Argentine market was open and the real ledger has two sales on
    it.

    Iterating over trading days alone dropped both silently. The only thing that caught it was
    the end-state reconciliation failing on exactly those two tickers.
    """
    holiday = "2026-01-19"
    rows = conversion("2026-01-15") + [
        LedgerRow(holiday, "sell", "AAPL", 100.0, "USD", 10.0, 1000.0),
    ]
    doc = document(rows, opening={"AAPL": 100.0})
    # Note: no bar on the holiday, which is exactly the situation.
    bars = make_bars({"AAPL": {"2026-01-15": 100.0, "2026-01-16": 100.0, "2026-01-20": 100.0}})

    result = reconstruct.build(doc, bars)
    assert date.fromisoformat(holiday) in [point.day for point in result.points]
    # The whole position was sold; it must not survive to the end.
    assert result.positions.get("AAPL", 0.0) == pytest.approx(0.0, abs=1e-9)


def test_calendar_is_the_union_not_the_intersection():
    rows = conversion("2026-01-05")
    bars = make_bars({"AAPL": {"2026-01-05": 1.0, "2026-01-06": 1.0}})
    days = reconstruct.calendar(
        bars, [date(2026, 1, 4)], date(2026, 1, 4), date(2026, 1, 6))
    assert days == [date(2026, 1, 4), date(2026, 1, 5), date(2026, 1, 6)]


# ---- the carry bucket ------------------------------------------------------------

def test_selling_a_carried_instrument_does_not_step_the_curve():
    """THE invariant the carry bucket exists for.

    A bond or a locally-listed equity has no US daily close, so it cannot be priced. Valuing
    it at zero would make its eventual sale look like money appearing from nowhere - a step
    up on 2026-01-23 that the account never earned. Carrying it at its transacted value
    instead makes the sale move value between two buckets and leave the total alone.
    """
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-07", "sell", "GGAL", 1000.0, "USD", 5.0, 5000.0),
    ]
    doc = document(rows, opening={"GGAL": 1000.0})
    bars = make_bars({"AAPL": dict.fromkeys(
        ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"), 100.0)})

    result = reconstruct.build(doc, bars)
    assert result.carried == ["GGAL"]
    assert result.opening_carry == pytest.approx(5000.0)

    values = {point.day: point.total_value for point in result.points}
    before = values[date(2026, 1, 6)]
    after = values[date(2026, 1, 8)]
    assert after == pytest.approx(before), "the sale invented value out of nothing"


def test_buying_a_carried_instrument_does_not_step_the_curve_either():
    """The mirror case - TGNO4, bought inside the window and still held at the end."""
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-06", "sell", "AAPL", 5.0, "USD", 10.0, 50.0),
        LedgerRow("2026-01-07", "buy", "TGNO4", 400.0, "USD", 0.1, 40.0),
    ]
    doc = document(rows)
    bars = make_bars({"AAPL": dict.fromkeys(
        ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"), 100.0)})

    result = reconstruct.build(doc, bars)
    values = {point.day: point.total_value for point in result.points}
    assert values[date(2026, 1, 8)] == pytest.approx(values[date(2026, 1, 6)])


# ---- cash ------------------------------------------------------------------------

def test_opening_cash_is_the_least_that_avoids_implied_borrowing():
    """The export records trades, not deposits, so the running balance starts at zero and goes
    negative the first time a purchase precedes the sale that funded it.

    Left alone that reads as margin the account never had - and `SessionDocument.cash_balance`
    is `ge=0`, so the loader would 422 on it.
    """
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-06", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-07", "sell", "AAPL", 10.0, "USD", 10.0, 100.0),
    ]
    bars = make_bars({"AAPL": dict.fromkeys(
        ("2026-01-05", "2026-01-06", "2026-01-07"), 100.0)})
    result = reconstruct.build(document(rows), bars)

    assert result.opening_cash == pytest.approx(100.0)
    assert min(point.cash_balance for point in result.points) >= -1e-9
    assert result.cash_balance >= 0.0


# ---- valuation -------------------------------------------------------------------

def test_value_tracks_the_daily_close():
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
    ]
    bars = make_bars({"AAPL": {
        "2026-01-05": 100.0, "2026-01-06": 110.0, "2026-01-07": 90.0,
    }})
    result = reconstruct.build(document(rows), bars)
    values = {point.day: point.positions_value for point in result.points}
    assert values[date(2026, 1, 5)] == pytest.approx(100.0)   # 1 share
    assert values[date(2026, 1, 6)] == pytest.approx(110.0)
    assert values[date(2026, 1, 7)] == pytest.approx(90.0)


def test_cost_basis_averages_on_buy_and_holds_on_sell():
    # The close has to move with the CEDEAR price, or the *ratio* moves instead and each
    # trade buys a different number of shares. Holding it at 10:1 isolates the cost logic.
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),   # 1 sh @ 100
        LedgerRow("2026-01-06", "buy", "AAPL", 10.0, "USD", 20.0, 200.0),   # 1 sh @ 200
        LedgerRow("2026-01-07", "sell", "AAPL", 10.0, "USD", 30.0, 300.0),  # sell 1 sh
    ]
    bars = make_bars({"AAPL": {
        "2026-01-05": 100.0, "2026-01-06": 200.0, "2026-01-07": 300.0,
    }})
    result = reconstruct.build(document(rows), bars)
    assert result.ratios["AAPL"] == pytest.approx(10.0)
    # Average cost, as portfolio._apply does it: the sale leaves the per-share basis alone.
    assert result.positions["AAPL"] == pytest.approx(1.0)
    assert result.cost_basis["AAPL"] == pytest.approx(150.0)


# ---- session shaping -------------------------------------------------------------

def test_session_excludes_what_post_session_would_reject():
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-05", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
        LedgerRow("2026-01-06", "sell", "GGAL", 100.0, "USD", 1.0, 100.0),
    ]
    doc = document(rows, opening={"GGAL": 100.0})
    bars = make_bars({"AAPL": {"2026-01-05": 100.0, "2026-01-06": 100.0}})
    result = reconstruct.build(doc, bars)

    body, dropped = session.to_session(result)
    tickers = [entry["ticker"] for entry in body["positions"]]
    assert tickers == ["AAPL"]
    # GGAL passes every ticker regex in the project, so the exclusion rule has to be
    # "has US daily closes" - otherwise it lands in db.tracked_tickers and the simulator
    # invents a GBM path for a stock that has no US listing.
    assert any(entry["ticker"] == "GGAL" for entry in dropped)
    assert body["cash_balance"] >= 0.0
    assert all(entry["quantity"] > 0 for entry in body["positions"])


# ---- against the committed artifact ---------------------------------------------

def test_committed_ledger_reconciles_exactly():
    """The end-state check, run against whatever is committed.

    Every step feeds the terminal position - the rate, the ratios, the openings, the calendar -
    so when they are all right this is exact, and when any one is wrong this is usually the
    only place it shows.
    """
    doc = ledger_module.load_document()
    if doc is None:
        pytest.skip("no committed ledger.json - run scripts/import_broker_with_dates")
    bars = bars_module.load()
    if not bars.tickers():
        pytest.skip("no bars cache")

    result = reconstruct.build(doc, bars)
    assert result.available
    problems = reconstruct.reconcile(result, doc)
    assert problems == [], f"{len(problems)} ticker(s) did not reconcile"
    assert result.points[0].total_value > 0


def test_committed_ledger_prices_or_carries_every_ticker():
    """Nothing may fall through the gap between the two buckets.

    Catches "someone added a ticker to the ledger and never fetched its bars" - the name would
    otherwise be neither priced nor carried, and simply vanish.
    """
    doc = ledger_module.load_document()
    if doc is None:
        pytest.skip("no committed ledger.json")
    result = reconstruct.build(doc, bars_module.load())
    if not result.available:
        pytest.skip("no bars cache")

    universe = set(doc.opening) | {row.ticker for row in doc.rows}
    accounted = set(result.priced) | set(result.carried)
    assert universe - accounted == set()


def test_bars_epoch_day_conversion():
    """An off-by-one here shifts the whole curve a day against every real price."""
    assert bars_module.EPOCH + timedelta(days=20430) == date(2025, 12, 8)


def test_bars_missing_ticker_is_none_not_a_keyerror():
    bars = make_bars({"AAPL": {"2026-01-05": 1.0}})
    assert bars.series("NOPE") is None
    assert bars.close_on("NOPE", date(2026, 1, 5)) is None


def test_bars_carry_the_previous_close_over_a_gap():
    """A weekend must be a flat segment, not a hole the chart interpolates across."""
    bars = make_bars({"AAPL": {"2026-01-05": 42.0}})
    assert bars.close_on("AAPL", date(2026, 1, 7)) == pytest.approx(42.0)
    assert bars.close_on("AAPL", date(2026, 1, 20)) is None      # beyond the lookback


# ---- trade events and held-since (the MP misreading) -----------------------------

def test_events_record_the_users_trades_in_share_terms():
    """REGRESSION of a misreading, not a computation: a recently-bought name drew its market
    price back to the start of the bars window with nothing marking the entry, and read as a
    long-held position. The events are what the markers draw from."""
    # The close moves with the CEDEAR price so the ratio observation stays 10:1 on both
    # trades - otherwise the median shifts and the share counts below drift with it.
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-06", "buy", "AAPL", 20.0, "USD", 10.0, 200.0),
        LedgerRow("2026-01-07", "sell", "AAPL", 10.0, "USD", 12.0, 120.0),
    ]
    bars = make_bars({"AAPL": {
        "2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 120.0,
    }})
    result = reconstruct.build(document(rows), bars)

    events = result.events["AAPL"]
    assert [event["side"] for event in events] == ["buy", "sell"]
    # Share equivalents and the actual converted fill price - not that day's close.
    assert events[0]["shares"] == pytest.approx(2.0)
    assert events[0]["price"] == pytest.approx(100.0)
    assert events[1]["shares"] == pytest.approx(1.0)
    assert events[1]["price"] == pytest.approx(120.0)
    # The FX conversion rows are currency moves, never trades - no AL30 events.
    assert "AL30" not in result.events


def test_held_since_is_the_buy_date_for_a_position_built_in_the_window():
    rows = conversion("2026-01-05") + [
        LedgerRow("2026-01-07", "buy", "AAPL", 10.0, "USD", 10.0, 100.0),
    ]
    bars = make_bars({"AAPL": dict.fromkeys(
        ("2026-01-05", "2026-01-06", "2026-01-07"), 100.0)})
    result = reconstruct.build(document(rows), bars)
    assert result.held_since["AAPL"] == "2026-01-07"


def test_held_since_is_the_window_start_for_an_opening_position():
    rows = conversion("2026-01-06") + [
        LedgerRow("2026-01-07", "sell", "GOOGL", 10.0, "USD", 10.0, 100.0),
    ]
    doc = document(rows, opening={"GOOGL": 100.0})
    bars = make_bars({"GOOGL": dict.fromkeys(
        ("2026-01-06", "2026-01-07"), 100.0)})
    result = reconstruct.build(doc, bars)
    # The curve starts at the first effective trade date (the FX pair on the 6th is netted
    # out), so "held since the window start" resolves to the 7th - the curve's left edge,
    # which is the earliest date the chart can honestly claim.
    assert result.held_since["GOOGL"] == "2026-01-07"
