"""Market data. The only import surface — nothing outside this package imports a submodule.

Consumers use exactly six things:

    service.price("MU")               # float | None  — fill price, valuation
    service.quote("MU")               # Quote | None  — full cache entry
    await service.add_ticker("PYPL")  # Quote | None  — immediate, priced before it returns
    await service.sync_tracked(watchlist | position_tickers)
    await service.validate("ASDF")    # bool — symbol exists at the provider
    service.mode, service.healthy     # Mode, bool — for /api/health and the SSE hello
"""

from __future__ import annotations

import logging
import os
import random
from typing import TypeVar

from .cache import HISTORY_MAXLEN, PriceCache
from .deps import get_service
from .massive import (
    FREE_TIER_RPM,
    PAID_TIER_RPM,
    HybridLiveSource,
    MassiveAnchorProvider,
    MassiveGateway,
    MassiveLiveSource,
    probe_entitlement,
)
from .models import Direction, Quote, Tick
from .routes import router
from .seeds import SEED_WATCHLIST
from .service import MarketDataService, current_session_date
from .simulator import GBMEngine, SimulatedSource, StaticAnchorProvider
from .source import AnchorProvider, Entitlement, MarketDataSource, Mode
from .symbols import InvalidTicker, normalize_ticker

log = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "AnchorProvider",
    "Direction",
    "Entitlement",
    "GBMEngine",
    "HISTORY_MAXLEN",
    "InvalidTicker",
    "MarketDataService",
    "MarketDataSource",
    "Mode",
    "PriceCache",
    "Quote",
    "SEED_WATCHLIST",
    "SimulatedSource",
    "StaticAnchorProvider",
    "Tick",
    "build_market_service",
    "current_session_date",
    "get_service",
    "normalize_ticker",
    "router",
]


def _env_number(name: str, default: T, cast, minimum: float | None = None) -> T:
    """Parse a numeric env var, falling back loudly rather than raising.

    `build_market_service` promises it never fails, and a deployment typo is exactly the
    case that promise exists for: `SIM_SEED=abc` used to raise `ValueError` before either
    simulated fallback could be built, taking down a startup path documented as
    always-degrading. A non-positive poll interval is the same class of problem one step
    later — it makes the market loop spin instead of sleeping.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %r", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("%s=%r is below the %s minimum; using %r", name, raw, minimum, default)
        return default
    return value


async def build_market_service(cache: PriceCache) -> MarketDataService:
    """Assemble the stack from the environment. NEVER raises on a bad key or bad config."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    seed = _env_number("SIM_SEED", None, int)

    if not api_key:
        log.info("MASSIVE_API_KEY unset -> SIMULATED mode (static seed prices)")
        return _simulated(cache, StaticAnchorProvider(random.Random(seed)),
                          Mode.SIMULATED, seed)

    try:
        gateway = MassiveGateway(api_key, rpm=FREE_TIER_RPM)
    except Exception as exc:                        # noqa: BLE001 — SDK missing or unusable
        log.warning("cannot construct the Massive client (%s) -> SIMULATED mode", exc)
        return _simulated(cache, StaticAnchorProvider(random.Random(seed)),
                          Mode.SIMULATED, seed)

    entitlement = await probe_entitlement(gateway)

    if entitlement is Entitlement.SNAPSHOTS:
        gateway.set_rpm(PAID_TIER_RPM)
        interval = _env_number("MARKET_POLL_INTERVAL_S", 15.0, float, minimum=0.1)
        log.info("Massive snapshots entitled -> LIVE mode (poll %.1fs)", interval)
        source: MarketDataSource = MassiveLiveSource(gateway, poll_interval=interval)
        if os.environ.get("MARKET_CLOSED_FALLBACK", "true").lower() == "true":
            source = HybridLiveSource(source, SimulatedSource(GBMEngine(seed)), gateway)
        return MarketDataService(source, MassiveAnchorProvider(gateway), cache, Mode.LIVE)

    if entitlement is Entitlement.AGGREGATES:
        log.info("Massive key is aggregates-only (Basic) -> ANCHORED mode: "
                 "real previous closes, simulated intraday motion")
        return _simulated(cache, MassiveAnchorProvider(gateway), Mode.ANCHORED, seed)

    log.warning("Massive key unusable (%s) -> SIMULATED mode", entitlement)
    return _simulated(cache, StaticAnchorProvider(random.Random(seed)), Mode.SIMULATED, seed)


def _simulated(
    cache: PriceCache,
    anchors: AnchorProvider,
    mode: Mode,
    seed: int | None,
) -> MarketDataService:
    return MarketDataService(
        source=SimulatedSource(GBMEngine(seed)), anchors=anchors, cache=cache, mode=mode,
    )
