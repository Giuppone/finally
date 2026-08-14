"""Ticker normalisation — D8. Without it `aapl` and `AAPL` become two watchlist rows, two
cache entries and two positions in the same stock (Review.md B1)."""

from __future__ import annotations

import pytest

from app.market import InvalidTicker, normalize_ticker


@pytest.mark.parametrize("raw,expected", [
    ("mu", "MU"),
    (" pypl ", "PYPL"),
    ("AAPL", "AAPL"),
    ("\tintc\n", "INTC"),
    ("BRK.B", "BRK.B"),
    ("brk.b", "BRK.B"),
    ("RDS-A", "RDS-A"),
])
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "TOOLONG", "not a ticker", "MU;DROP", "M U", "123", "MU.", "MU-",
    "BRK.BBB", "$MU", None,
])
def test_rejects(raw) -> None:
    with pytest.raises(InvalidTicker):
        normalize_ticker(raw)


def test_invalid_ticker_is_a_value_error() -> None:
    # Callers that only catch ValueError still behave sanely.
    assert issubclass(InvalidTicker, ValueError)
