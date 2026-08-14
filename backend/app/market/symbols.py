"""Ticker normalisation. A leaf module — imports nothing from the package (design §3)."""

from __future__ import annotations

import re

TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")


class InvalidTicker(ValueError):
    """Raised at every boundary that accepts a user- or LLM-supplied symbol."""


def normalize_ticker(raw: str) -> str:
    """The ONE normalisation point (D8). Apply at every API boundary, in the LLM action
    executor, and before any cache or DB lookup.

    SQLite's default collation is case-sensitive, so without this `aapl` and `AAPL` become
    two watchlist rows, two cache entries and two positions in the same stock — a heatmap
    showing one holding twice (Review.md B1). Declare the column `COLLATE NOCASE` as a
    backstop, but do not rely on it: the cache and the GBM engine are plain dicts.
    """
    ticker = (raw or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise InvalidTicker(f"invalid ticker: {raw!r}")
    return ticker
