"""The two abstractions: sources produce prices, one service owns the loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Protocol

from .models import Tick


class Mode(StrEnum):
    SIMULATED = "simulated"
    ANCHORED = "anchored"
    LIVE = "live"


class Entitlement(StrEnum):
    SNAPSHOTS = "snapshots"     # Starter+  -> LIVE
    AGGREGATES = "aggregates"   # Basic     -> ANCHORED
    NONE = "none"               # bad key   -> SIMULATED


class AnchorProvider(Protocol):
    """Supplies the session anchor for tickers entering the tracked set, and answers
    'is this a real symbol?' — both are provider knowledge, so they live together."""

    async def anchors(self, tickers: list[str], session_date: str) -> dict[str, float]:
        """Return {ticker: anchor_price}. Omit tickers that cannot be resolved."""
        ...

    async def is_known(self, ticker: str, session_date: str) -> bool:
        """True if the provider recognises the symbol (D9).

        Takes the session date so validation shares the same cached universe the anchor
        lookup uses, instead of populating it under a different key and paying for the
        load twice.
        """
        ...

    async def refresh(self, session_date: str) -> None:
        """Invalidate any per-session state. Called at the session roll."""
        ...


class MarketDataSource(ABC):
    """Produces prices for a set of tickers. Owns no loop, no cache, no tracked set."""

    #: Seconds between service calls to `poll()`.
    poll_interval: float = 0.5

    @abstractmethod
    async def prime(self, tickers: list[str], anchors: dict[str, float]) -> None:
        """Register tickers and their anchors before the first poll."""

    @abstractmethod
    async def poll(self, tickers: list[str]) -> list[Tick]:
        """Return the current price for each ticker. Called every `poll_interval`."""

    @abstractmethod
    async def release(self, ticker: str) -> None:
        """Drop per-ticker state. Called when a ticker leaves the tracked set."""

    async def rebase(self, ticker: str, price: float) -> None:
        """Move a ticker's price to `price` at a session roll. Default: no-op (a live
        source has nothing to rebase — the API is authoritative)."""
        return None

    async def aclose(self) -> None:
        """Release process-wide resources. Default: nothing."""
        return None
