"""Parsing the dated broker export, and the exchange rate hidden inside it.

No network, no container, no app: every function here is pure and fed a string or a dict.
"""

from __future__ import annotations

import json

import pytest

from app.history import fx, ledger

HEADER = "Fecha\tTipo\tTicker\tCantidad\tMoneda\tPrecio\tNeto"


def sheet(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def row(date: str, kind: str, ticker: str, qty: str, ccy: str, price: str, net: str) -> str:
    return "\t".join([date, kind, ticker, qty, ccy, price, net])


# ---- parsing ---------------------------------------------------------------------

def test_parses_buys_and_sells():
    rows, ignored, warnings = ledger.parse_ledger(sheet(
        row("2026-01-12", "Compra", "MU", "40", "ARS", "300000.0", "12000000.0"),
        row("2026-01-13", "Venta", "MU", "4", "USD", "200.0", "800.0"),
    ))
    assert warnings == [] and ignored == []
    assert [r.kind for r in rows] == ["buy", "sell"]
    assert rows[0].quantity == 40 and rows[0].currency == "ARS"
    assert rows[1].price == 200.0


def test_income_rows_are_reported_not_silently_dropped():
    """The instruction was to ignore dividends - not to pretend they were never there.

    A silent drop and a deliberate one look identical in the output, and the difference
    matters the first time someone asks why the cash does not tie out.
    """
    rows, ignored, _ = ledger.parse_ledger(sheet(
        row("2026-06-15", "Dividendos Cash", "META", "100", "USD", "0.0", "1.23"),
        row("2026-01-08", "Renta", "AL30", "5000", "USD", "0.0", "15.00"),
        row("2026-01-08", "Amortizacion", "AL30", "5000", "USD", "0.0", "400.00"),
        row("2026-01-12", "Compra", "MU", "4", "USD", "200.0", "800.0"),
    ))
    assert len(rows) == 1
    assert [entry["category"] for entry in ignored] == ["dividend", "coupon", "amortisation"]


def test_unknown_transaction_type_is_fatal():
    """Skipping a whole category of transaction misstates the book with no visible symptom.

    A malformed single row is recoverable; an unrecognised *kind* is not, because there is no
    way to tell whether it moved a position.
    """
    with pytest.raises(ledger.LedgerError, match="unknown transaction type"):
        ledger.parse_ledger(sheet(
            row("2026-01-12", "Canje", "AL30", "100", "USD", "1.0", "100.0"),
        ))


def test_malformed_row_is_skipped_and_reported():
    rows, _, warnings = ledger.parse_ledger(sheet(
        row("2026-01-12", "Compra", "MU", "not-a-number", "USD", "200.0", "800.0"),
        row("2026-01-13", "Compra", "MU", "4", "USD", "200.0", "800.0"),
    ))
    assert len(rows) == 1
    assert len(warnings) == 1 and "not a number" in warnings[0]


def test_missing_column_is_fatal():
    with pytest.raises(ledger.LedgerError, match="missing column"):
        ledger.parse_ledger("Fecha\tTipo\tTicker\n2026-01-12\tCompra\tMU\n")


def test_digit_bearing_tickers_are_accepted():
    """AL30 and TGNO4 are the reason this module has its own normaliser.

    `market.symbols.normalize_ticker` is `[A-Z]{1,5}` - letters only - so it rejects every
    Argentine bond in the file. Those bond rows are exactly what the exchange rate is measured
    from, so rejecting them here would throw the rate away before it could be derived.
    """
    for symbol in ("AL30", "GD35", "AE38", "S29Y6", "TGNO4"):
        assert ledger.normalize_symbol(symbol.lower()) == symbol
    with pytest.raises(ledger.LedgerError):
        ledger.normalize_symbol("not a ticker")


def test_ledger_numbers_are_not_argentine_formatted():
    """The ledger and the holdings export disagree about what `305.650` means.

    Holdings export (Argentine grouping): 305.650 == three hundred five thousand six hundred
    fifty. Ledger (plain dot-decimal): 8610.0 == eight thousand six hundred and ten.

    Reading one with the other's parser is a 1000x error and completely silent. This test
    exists so that nobody unifies `parse_amount` and this module's `float()`.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from portfolio_tool import parse_amount

    assert parse_amount("305.650") == 305650.0
    rows, _, _ = ledger.parse_ledger(sheet(
        row("2026-01-12", "Compra", "MU", "1", "ARS", "305.650", "305.65"),
    ))
    assert rows[0].price == 305.65
    assert rows[0].price != parse_amount("305.650")


# ---- the opening book ------------------------------------------------------------

def test_back_solve_openings():
    rows, _, _ = ledger.parse_ledger(sheet(
        row("2026-01-12", "Venta", "NVDA", "90", "ARS", "12000.0", "1080000.0"),
        row("2026-06-30", "Compra", "NVDA", "200", "ARS", "13000.0", "2600000.0"),
        row("2026-02-01", "Compra", "ALAB", "500", "ARS", "5000.0", "2500000.0"),
    ))
    openings, warnings = ledger.back_solve_openings({"NVDA": 300.0, "ALAB": 500.0}, rows)
    # NVDA net flow is +110, so 300 today means 190 were already held.
    assert openings["NVDA"] == pytest.approx(190.0)
    # ALAB was built entirely inside the window - the strongest signal the two files agree.
    assert openings["ALAB"] == pytest.approx(0.0)
    assert warnings == []


def test_negative_opening_is_reported_not_clamped_silently():
    rows, _, _ = ledger.parse_ledger(sheet(
        row("2026-01-12", "Compra", "MU", "100", "USD", "10.0", "1000.0"),
    ))
    openings, warnings = ledger.back_solve_openings({"MU": 10.0}, rows)
    assert openings["MU"] == 0.0
    assert len(warnings) == 1 and "opening position solves to" in warnings[0]


# ---- the exchange rate -----------------------------------------------------------

def test_fx_from_same_day_conversion_pair():
    """Buy the bond in dollars, sell the same bond in pesos the same day: that ratio is the
    rate the account actually transacted at."""
    rows, _, _ = ledger.parse_ledger(sheet(
        row("2026-07-07", "Compra", "AL30", "2000", "USD", "0.65", "1300.00"),
        row("2026-07-07", "Venta", "AL30", "2000", "ARS", "988.0", "1976000.00"),
    ))
    points, consumed = fx.observations(rows)
    assert len(points) == 1
    assert points[0][1] == pytest.approx(1976000.00 / 1300.00)
    # Both rows are consumed: they moved currency, not holdings. Counting them as a real buy
    # and a real sell nets to zero units but double-counts the cash.
    assert consumed == frozenset({0, 1})


@pytest.mark.parametrize("second, why", [
    (row("2026-07-07", "Venta", "AL30", "2000", "USD", "0.65", "1300.00"), "same currency"),
    (row("2026-07-07", "Compra", "AL30", "2000", "ARS", "988.0", "1976000.00"), "same side"),
    (row("2026-07-07", "Venta", "AL30", "999", "ARS", "988.0", "987012.0"), "different size"),
    (row("2026-07-07", "Venta", "GD35", "2000", "ARS", "988.0", "1976000.00"), "different bond"),
    (row("2026-07-08", "Venta", "AL30", "2000", "ARS", "988.0", "1976000.00"), "different day"),
])
def test_near_misses_are_not_conversion_pairs(second, why):
    """All four conditions are load-bearing. Drop any one and real trades start matching."""
    rows, _, _ = ledger.parse_ledger(sheet(
        row("2026-07-07", "Compra", "AL30", "2000", "USD", "0.65", "1300.00"), second,
    ))
    points, consumed = fx.observations(rows)
    assert points == [], why
    assert consumed == frozenset(), why


def test_fx_curve_interpolates_and_stays_flat_outside():
    rate_on = fx.curve([("2026-01-01", 1400.0), ("2026-01-11", 1500.0)])
    assert rate_on("2026-01-01") == pytest.approx(1400.0)
    assert rate_on("2026-01-06") == pytest.approx(1450.0)
    assert rate_on("2026-01-11") == pytest.approx(1500.0)
    # Flat, not extrapolated: the peso only depreciates, so a linear extension off the last
    # two points would keep depreciating forever and quietly inflate every USD figure.
    assert rate_on("2025-06-01") == pytest.approx(1400.0)
    assert rate_on("2027-06-01") == pytest.approx(1500.0)


def test_fx_curve_needs_at_least_one_observation():
    with pytest.raises(ValueError, match="no ARS/USD observations"):
        fx.curve([])


# ---- the committed document ------------------------------------------------------

def test_document_round_trips(tmp_path):
    rows, ignored, _ = ledger.parse_ledger(sheet(
        row("2026-01-12", "Compra", "MU", "40", "ARS", "300000.0", "12000000.0"),
        row("2026-06-15", "Dividendos Cash", "META", "100", "USD", "0.0", "1.23"),
    ))
    document = ledger.LedgerDocument(
        rows=rows, opening={"MU": 36.0}, snapshot_date="2026-08-10",
        snapshot={"MU": ledger.Holding(quantity=40.0, price_ars=300000.0)},
        ignored=ignored, source="test",
    )
    path = tmp_path / "ledger.json"
    path.write_text(ledger.to_json(document), encoding="utf-8")

    restored = ledger.load_document(path)
    assert restored is not None
    assert restored.rows == document.rows
    assert restored.opening == {"MU": 36.0}
    assert restored.snapshot["MU"].price_ars == 300000.0


def test_missing_document_is_none_not_an_error(tmp_path):
    """A fresh checkout has never run the importer; the app must still boot."""
    assert ledger.load_document(tmp_path / "absent.json") is None


def test_unknown_document_version_is_rejected(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"version": 99, "rows": []}), encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="version 99"):
        ledger.load_document(path)
