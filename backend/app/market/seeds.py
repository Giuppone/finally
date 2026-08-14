# Calibrated from Massive daily bars, 2025-12-01 -> 2026-08-07 (pulled 2026-08-10).
# Regenerate with scripts/calibrate_market.py — see MARKET_SIMULATOR.md §9.
# This file is DATA. No logic, no imports from the rest of the package.

from __future__ import annotations

# PLAN.md §7 default watchlist. Bare exchange symbols only — INTC, not "INTEL".
SEED_WATCHLIST: tuple[str, ...] = (
    "ALAB", "MRVL", "MU", "AMD", "INTC", "PLTR", "ANET", "LRCX", "AMAT", "SLV",
)

SEED_PRICES: dict[str, float] = {
    "ALAB": 334.17, "MRVL": 218.72, "MU": 877.57, "AMD": 483.36, "INTC": 101.65,
    "PLTR": 172.01, "ANET": 188.67, "LRCX": 311.35, "AMAT": 539.14, "SLV": 57.50,
}

# sigma = realised annualised vol; mu = DAMPED drift (~10% of realised, cap 0.20).
# Using realised mu (0.24–2.30) would make every position profitable and the
# heatmap's red/green encoding meaningless — MARKET_SIMULATOR.md §6.
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "ALAB": {"sigma": 1.060, "mu": 0.16},
    "MRVL": {"sigma": 0.839, "mu": 0.16},
    "MU":   {"sigma": 0.885, "mu": 0.20},
    "AMD":  {"sigma": 0.720, "mu": 0.14},
    "INTC": {"sigma": 0.835, "mu": 0.17},
    "PLTR": {"sigma": 0.629, "mu": 0.02},
    "ANET": {"sigma": 0.573, "mu": 0.07},
    "LRCX": {"sigma": 0.692, "mu": 0.13},
    "AMAT": {"sigma": 0.644, "mu": 0.13},
    "SLV":  {"sigma": 0.749, "mu": 0.04},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.45, "mu": 0.05}
FALLBACK_PRICE_RANGE: tuple[float, float] = (40.0, 400.0)   # unknown ticker, no real anchor

SECTORS: dict[str, str] = {
    "ALAB": "semi", "MRVL": "semi", "MU": "semi", "AMD": "semi", "INTC": "semi",
    "LRCX": "semicap", "AMAT": "semicap",
    "ANET": "networking",
    "PLTR": "software",
    "SLV": "commodity",
}

# Measured correlations, encoded as blocks so user-added tickers inherit sane values.
SECTOR_RHO: dict[tuple[str, str], float] = {
    ("semi", "semi"):          0.55,
    ("semicap", "semicap"):    0.90,   # LRCX/AMAT realised 0.92 — near duplicates
    ("semi", "semicap"):       0.65,
    ("networking", "semi"):    0.45,
    ("networking", "semicap"): 0.50,
    ("software", "*"):         0.15,   # PLTR: near-independent of this basket
    ("commodity", "*"):        0.25,   # SLV: macro only
}
DEFAULT_RHO = 0.35
