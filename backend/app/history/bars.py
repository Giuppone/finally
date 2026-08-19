"""Daily closes, read from the calibration bars cache.

STDLIB ONLY - see the note at the top of `ledger.py`.

`backend/calibration/bars.json` is written by `scripts/calibrate_market.py`, committed, and
copied into the image by the Dockerfile's `COPY backend/ ./`. Until now only that script read
it; the running app consumed the generated `app/market/seeds.py` instead. This module makes
the cache readable at runtime as well.

Why read the JSON directly rather than seed a `daily_bars` SQLite table:

  * `/app/db` is a named volume. Seed-on-first-run means an image rebuild shipping fresher
    bars has no effect, because the volume already has rows - and `init_db` is a single
    `executescript` with no migration framework to reconcile that with.
  * These bars are immutable reference data versioned with the code, not user state. Putting
    them in the user-state store is a category error, and `POST /api/portfolio/reset` would
    have to learn to leave one table alone.
  * It is 68 KB. The read is milliseconds, once per process.

Only `closes` is read. `start`/`end`/`fetched_at` are surfaced as metadata but never gate
pricing, so this module stays independent of calibrate_market's window semantics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# How far back to look for a close when a date has no bar. Covers a weekend plus a long
# holiday break; beyond that the ticker genuinely was not trading.
LOOKBACK_DAYS = 7

EPOCH = date(1970, 1, 1)


def bars_path() -> Path:
    """`backend/calibration/bars.json`, overridable for tests.

    Same `parents[2]` reasoning as `ledger.default_path()`, and the same env-override
    convention as `db.db_path()`'s `FINALLY_DB_PATH`.
    """
    override = os.environ.get("FINALLY_BARS_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "calibration" / "bars.json"


@dataclass(frozen=True)
class Bars:
    """Parsed daily closes, keyed by ticker then calendar date."""

    closes: dict[str, dict[date, float]]
    days: tuple[date, ...]
    fetched_at: str

    def series(self, ticker: str) -> dict[date, float] | None:
        return self.closes.get(ticker.upper())

    def close_on(self, ticker: str, when: date, lookback: int = LOOKBACK_DAYS) -> float | None:
        """The close on `when`, else the most recent one within `lookback` days.

        Carrying the previous close forward is what makes a weekend or a market holiday a flat
        segment rather than a hole. Returning None there instead would drop the point and let
        the chart interpolate across the gap, which draws a line the market never traded.
        """
        series = self.series(ticker)
        if not series:
            return None
        for back in range(lookback + 1):
            price = series.get(when - timedelta(days=back))
            if price is not None:
                return price
        return None

    @property
    def as_of(self) -> date | None:
        return self.days[-1] if self.days else None

    @property
    def start(self) -> date | None:
        return self.days[0] if self.days else None

    def tickers(self) -> list[str]:
        return sorted(self.closes)


_EMPTY = Bars(closes={}, days=(), fetched_at="")

# Memoised on file identity, not on a boolean: editing bars.json during development and
# reloading the page picks the change up with no restart, while the container - where the file
# is baked in and never changes - parses exactly once per process.
_cache: tuple[tuple[str, int, int], Bars] | None = None


def _stamp(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load(path: Path | None = None) -> Bars:
    """The bars cache, or an empty one when the file is missing or unreadable.

    Never raises. A missing bars.json degrades every history route to `available: false`;
    it must not stop the app booting or take down the live market stream with it.
    """
    global _cache
    target = path or bars_path()
    stamp = _stamp(target)
    if stamp is None:
        return _EMPTY
    if _cache is not None and _cache[0] == stamp:
        return _cache[1]

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _EMPTY

    closes: dict[str, dict[date, float]] = {}
    every: set[date] = set()
    fetched = ""
    for ticker, entry in (payload.get("tickers") or {}).items():
        series: dict[date, float] = {}
        for pair in entry.get("closes") or []:
            try:
                epoch_day, close = int(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            # `t // 86_400_000` in calibrate_market.fetch_bars, i.e. days since 1970-01-01.
            # An off-by-one here shifts the entire curve by a day against every real price.
            when = EPOCH + timedelta(days=epoch_day)
            series[when] = close
        if series:
            closes[ticker.upper()] = series
            every.update(series)
        fetched = max(fetched, str(entry.get("fetched_at") or ""))

    bars = Bars(closes=closes, days=tuple(sorted(every)), fetched_at=fetched)
    _cache = (stamp, bars)
    return bars


def reset_cache() -> None:
    """Drop the memo. For tests that swap the file under a single process."""
    global _cache
    _cache = None
