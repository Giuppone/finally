#!/usr/bin/env python3
"""Recalibrate the market model from real daily bars, and regenerate `app/market/seeds.py`.

This is the script `MARKET_SIMULATOR.md` section 9 specifies and `seeds.py`'s own header points at
("Regenerate with scripts/calibrate_market.py"), which had never been written.

    calibrate_market.py --dry-run                 # show what would change, fetch nothing new
    calibrate_market.py --yes                     # fetch what is missing, rewrite seeds.py
    calibrate_market.py --tickers COIN,PANW --yes # add two names; the rest come from cache

WHY A CACHE. Correlations need *aligned daily return series*, so recomputing after adding one
ticker needs every other ticker's series too - summary statistics are not enough. The raw
closes are therefore cached in `backend/calibration/bars.json` (~2 KB per ticker). Adding a
ticker then costs exactly one API call instead of twenty-five, which matters: a Basic key
allows 5 requests a minute, so a cold run over 25 tickers takes about five minutes.

STDLIB ONLY, and no use of the `massive` SDK, deliberately. The SDK's `list_aggs` is a
generator that auto-paginates, so it can spend several HTTP requests per ticker - which makes
the request count, and therefore the rate limiting, impossible to reason about on a 5/min
budget. One explicit request per ticker with `limit=50000` fits any 8-month range in a single
page and makes the budget exact. section 9 records that the original run did the same, sleeping 13
seconds between calls.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BARS_CACHE = REPO_ROOT / "backend" / "calibration" / "bars.json"
SEEDS_FILE = REPO_ROOT / "backend" / "app" / "market" / "seeds.py"

API_BASE = "https://api.polygon.io"
TRADING_DAYS = 252
CACHE_VERSION = 1

# 5 requests/minute on a Basic key (MASSIVE_API.md §2). 13s, not 12, for the same reason the
# app's limiter is a sliding window rather than a token bucket: a bucket that starts full
# lets a sixth request through inside the same rolling minute.
SECONDS_BETWEEN_CALLS = 13.0

# §9: "reject a run where any ticker returns fewer than ~60 bars". A short series produces a
# confident-looking sigma from almost no evidence.
MIN_BARS = 60

DEFAULT_DAYS = 250


class CalibrationError(RuntimeError):
    pass


# ---- the maths: pure functions, unit-tested in tests/test_calibrate.py --------

def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def annualised_sigma(returns: list[float]) -> float:
    """Annualised volatility from daily log returns.

    This is TOTAL volatility, which is what `TICKER_PARAMS` stores. The simulator subtracts
    its jump-variance budget at engine-build time via `diffusion_sigma(target, jump_var)` -
    so pre-subtracting here would remove it twice and render the whole watchlist too calm.
    """
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def annualised_drift(returns: list[float]) -> float:
    """Annualised mean LOG return - the realised mu of MARKET_SIMULATOR.md §2.

    Not CAGR. The two are related by `CAGR = exp(drift) - 1` and diverge violently on this
    basket: MU's log-drift is 2.299 where its CAGR is 8.96. The GBM engine's mu is a
    log-space drift, so this is the quantity it wants.
    """
    if not returns:
        return 0.0
    return statistics.fmean(returns) * TRADING_DAYS


def damp(mu_realised: float) -> float:
    """MARKET_SIMULATOR.md §6: mu ~ 10% of realised, capped at 0.20.

    The realised figures run 0.24 to 2.30 - the artefact of an eight-month AI-semiconductor
    melt-up. Feeding those in compounds every position upward forever, which makes the P&L
    chart a monotonic ramp and the heatmap's red/green encoding meaningless. Drift is also
    nearly invisible at demo timescales (it scales with t, volatility with sqrt(t)), so
    removing 90% of it costs almost nothing visually and buys a two-sided market.

    Verified against every shipped value: MU 2.299 -> 0.20 (capped), ALAB 1.600 -> 0.16,
    INTC 1.722 -> 0.17, PLTR 0.237 -> 0.02, SLV 0.414 -> 0.04.

    Negative drift passes through unchanged - only the upside is capped. A name that fell
    over the window should drift down in the simulator too; that is what keeps the market
    two-sided, which is the whole point of damping in the first place. The `+ 0.0` turns
    IEEE negative zero into plain zero, so a barely-negative drift renders as `0.0` rather
    than a puzzling `-0.00`.
    """
    return min(round(0.1 * mu_realised, 2), 0.20) + 0.0


def cagr(first_close: float, last_close: float, calendar_days: int) -> float:
    """Compound annual growth over the window. DISPLAY ONLY - never feeds the model.

    This is the number shown beside the damped drift so the damping is auditable rather than
    hidden. Feeding it to the simulator would be a units error: it is a simple return where
    the engine wants a log-space drift.
    """
    if first_close <= 0 or last_close <= 0 or calendar_days <= 0:
        return 0.0
    return (last_close / first_close) ** (365.0 / calendar_days) - 1.0


def pearson(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    da = [x - mean_a for x in a]
    db = [y - mean_b for y in b]
    denominator = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    if denominator <= 0:
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(da, db)) / denominator))


def align(series: dict[str, dict[int, float]]) -> tuple[list[int], dict[str, list[float]]]:
    """Reduce per-ticker {day: close} maps to a common set of days.

    Everything - sigma, drift, CAGR and every correlation - is computed on this one
    intersection. Estimating sigma over a ticker's full history while correlating over a
    shorter overlap would produce a covariance matrix whose diagonal and off-diagonal come
    from different periods, which is not a covariance matrix of anything.
    """
    if not series:
        return [], {}
    common = set.intersection(*(set(days) for days in series.values()))
    ordered = sorted(common)
    return ordered, {ticker: [days[d] for d in ordered] for ticker, days in series.items()}


@dataclass(frozen=True)
class Measured:
    ticker: str
    sigma: float
    mu_realised: float
    mu: float
    cagr: float
    last_close: float
    bars: int


def measure(ticker: str, closes: list[float], calendar_days: int) -> Measured:
    returns = log_returns(closes)
    realised = annualised_drift(returns)
    return Measured(
        ticker=ticker,
        sigma=annualised_sigma(returns),
        mu_realised=realised,
        mu=damp(realised),
        cagr=cagr(closes[0], closes[-1], calendar_days),
        last_close=closes[-1],
        bars=len(closes),
    )


# ---- the bars cache ----------------------------------------------------------

def load_cache(path: Path = BARS_CACHE) -> dict:
    if not path.is_file():
        return {"version": CACHE_VERSION, "tickers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != CACHE_VERSION:
        raise CalibrationError(
            f"{path} is version {data.get('version')}, this script writes {CACHE_VERSION}"
        )
    return data


def save_cache(cache: dict, path: Path = BARS_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True) + "\n",
                    encoding="utf-8")


def is_fresh(entry: dict | None, start: str, end: str, max_age_days: int) -> bool:
    """True when a cached entry already covers the window and is recent enough to reuse.

    Covering the window matters as much as age: a cached 3-month pull cannot answer an
    8-month request, however recently it was fetched.
    """
    if not entry or not entry.get("closes"):
        return False
    if entry.get("start", "9999") > start or entry.get("end", "0000") < end:
        return False
    fetched = entry.get("fetched_at")
    if not fetched:
        return False
    try:
        age = datetime.now(tz=timezone.utc) - datetime.fromisoformat(fetched)
    except ValueError:
        return False
    return age <= timedelta(days=max_age_days)


# ---- fetching ----------------------------------------------------------------

def api_key() -> str:
    """Environment first, then the project `.env`.

    PLAN.md §5's "the backend reads os.environ only" is a rule about the *service* - it must
    not depend on a file that does not exist inside the container. This is a developer script
    run from a checkout, where silently ignoring the .env everything else uses would just be
    a papercut.
    """
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MASSIVE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    print(f"using MASSIVE_API_KEY from {env_file}")
                    return key
    raise CalibrationError(
        "no MASSIVE_API_KEY in the environment or .env - calibration needs a Massive key"
    )


def fetch_bars(ticker: str, start: str, end: str, key: str) -> list[tuple[int, float]]:
    """One request, one page. Returns [(epoch_day, close)] ascending."""
    query = urllib.parse.urlencode({
        "adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key,
    })
    url = f"{API_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        # 403 is permanent for the life of a key (MASSIVE_API.md §6) - never retry it.
        raise CalibrationError(f"{ticker}: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise CalibrationError(f"{ticker}: {exc.reason}") from exc

    results = payload.get("results") or []
    if not results:
        raise CalibrationError(
            f"{ticker}: no bars returned ({payload.get('message') or payload.get('status')})"
        )
    return [(bar["t"] // 86_400_000, round(float(bar["c"]), 4)) for bar in results]


def previous_trading_day(today: date | None = None) -> date:
    """Basic tier rejects a `to` of today - "Your plan doesn't include this data timeframe".
    Step back over the weekend too, so a Sunday run does not ask for Saturday."""
    day = (today or date.today()) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ---- regenerating seeds.py ---------------------------------------------------

def render_seeds(measured: dict[str, Measured], correlations: dict[tuple[str, str], float],
                 window: dict, previous: dict) -> str:
    """Emit `app/market/seeds.py`.

    The prose is carried through deliberately. A generated file that drops the reasoning is
    a file the next person cannot judge - and the two things most worth explaining here (why
    the drift is damped, why sigma is the total) are exactly the two most likely to be
    "corrected" by someone who does not know.
    """
    tickers = sorted(measured)
    width = max(len(t) for t in tickers) + 2

    def block(rows: list[str]) -> str:
        return "\n".join(rows)

    params = block([
        f'    {(chr(34) + t + chr(34) + ":"):<{width + 2}} '
        f'{{"sigma": {measured[t].sigma:.3f}, "mu": {measured[t].mu:.2f}}},'
        for t in tickers
    ])
    prices = block([
        f'    "{t}": {measured[t].last_close:g},' for t in tickers
    ])
    cagrs = block([
        f'    "{t}": {measured[t].cagr:.4f},' for t in tickers
    ])
    rhos = block([
        f'    ("{a}", "{b}"): {value:.2f},'
        for (a, b), value in sorted(correlations.items())
    ])

    changes = []
    for ticker in tickers:
        before = previous.get(ticker)
        after = measured[ticker]
        if before is None:
            changes.append(f"#   + {ticker:<6} NEW   sigma {after.sigma:.3f}  mu {after.mu:.2f}")
        elif abs(before["sigma"] - after.sigma) > 0.0005 or abs(before["mu"] - after.mu) > 0.005:
            changes.append(
                f"#     {ticker:<6} sigma {before['sigma']:.3f} -> {after.sigma:.3f}"
                f"   mu {before['mu']:.2f} -> {after.mu:.2f}"
            )
    diff = "\n".join(changes) if changes else "#   (no parameter moved)"

    return f'''# Calibrated from Massive daily bars, {window["start"]} -> {window["end"]}
# ({window["trading_days"]} bars, pulled {window["pulled"]}).
#
# GENERATED by scripts/calibrate_market.py - see MARKET_SIMULATOR.md §9. Hand edits are lost
# on the next run. This file is DATA. No logic, no imports from the rest of the package.
#
# Changes in this run:
{diff}

from __future__ import annotations

# PLAN.md §7 default watchlist. Bare exchange symbols only - INTC, not "INTEL".
# Deliberately NOT every calibrated ticker: this seeds a new database, and the other
# entries below exist so tickers the user adds later are priced from real data.
SEED_WATCHLIST: tuple[str, ...] = (
    "ALAB", "MRVL", "MU", "AMD", "INTC", "PLTR", "ANET", "LRCX", "AMAT", "SLV",
)

# The window every number below was measured over. Surfaced in the risk panel so a reader
# can see how old the model is.
CALIBRATION_WINDOW: dict[str, str | int] = {{
    "start": "{window["start"]}",
    "end": "{window["end"]}",
    "trading_days": {window["trading_days"]},
    "pulled": "{window["pulled"]}",
}}

SEED_PRICES: dict[str, float] = {{
{prices}
}}

# sigma = realised annualised vol, TOTAL - the simulator subtracts its jump-variance budget
# at engine-build time via diffusion_sigma(), so this must not be pre-subtracted.
#
# mu = DAMPED drift: ~10% of realised, capped at 0.20 (MARKET_SIMULATOR.md §6). Using the
# realised figures (0.24-2.30 over this window) would compound every position upward
# forever, making the P&L chart a monotonic ramp and the heatmap's red/green meaningless.
# The undamped growth is in TICKER_CAGR below, and the risk panel shows it alongside.
TICKER_PARAMS: dict[str, dict[str, float]] = {{
{params}
}}

# Realised compound annual growth over the window. DISPLAY ONLY - shown beside the damped
# drift so the damping is auditable. Never feed this to the simulator: it is a simple
# return where the engine wants a log-space drift, and on this basket the two differ by
# nearly a factor of four.
TICKER_CAGR: dict[str, float] = {{
{cagrs}
}}

DEFAULT_PARAMS: dict[str, float] = {{"sigma": 0.45, "mu": 0.05}}
FALLBACK_PRICE_RANGE: tuple[float, float] = (40.0, 400.0)   # unknown ticker, no real anchor

SECTORS: dict[str, str] = {{
    "ALAB": "semi", "MRVL": "semi", "MU": "semi", "AMD": "semi", "INTC": "semi",
    "LRCX": "semicap", "AMAT": "semicap",
    "ANET": "networking",
    "PLTR": "software",
    "SLV": "commodity",
}}

# Measured pairwise correlations of daily log returns, over the window above. Consulted
# FIRST; the sector blocks below cover any pair where one side was never calibrated, which
# is what still lets the model extend to a ticker the user adds tomorrow.
MEASURED_RHO: dict[tuple[str, str], float] = {{
{rhos}
}}

# Correlations encoded as blocks, so user-added tickers inherit sane values.
SECTOR_RHO: dict[tuple[str, str], float] = {{
    ("semi", "semi"):          0.55,
    ("semicap", "semicap"):    0.90,   # LRCX/AMAT realised 0.92 - near duplicates
    ("semi", "semicap"):       0.65,
    ("networking", "semi"):    0.45,
    ("networking", "semicap"): 0.50,
    ("software", "*"):         0.15,   # PLTR: near-independent of this basket
    ("commodity", "*"):        0.25,   # SLV: macro only
}}
DEFAULT_RHO = 0.35
'''


# ---- the command -------------------------------------------------------------

def resolve_tickers(explicit: str | None, cache: dict) -> list[str]:
    """Everything already cached, plus the seed watchlist, plus whatever was asked for.

    Cached tickers are always included: they cost nothing (no API call) and dropping one
    would silently shrink the correlation matrix the rest depend on.
    """
    from app.market.seeds import SEED_WATCHLIST

    wanted = set(SEED_WATCHLIST) | set(cache.get("tickers", {}))
    if explicit:
        wanted |= {t.strip().upper() for t in explicit.split(",") if t.strip()}
    return sorted(wanted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate_market.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tickers", help="comma-separated names to ADD to the calibration")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"calendar days of history (default {DEFAULT_DAYS})")
    parser.add_argument("--max-age", type=int, default=30,
                        help="refetch a cached ticker older than this many days (default 30)")
    parser.add_argument("--force", action="store_true", help="refetch everything")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch nothing new and write nothing; report from cache")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    args = parser.parse_args(argv)

    end = previous_trading_day()
    start = end - timedelta(days=args.days)
    window_start, window_end = start.isoformat(), end.isoformat()

    cache = load_cache()
    tickers = resolve_tickers(args.tickers, cache)
    entries = cache.setdefault("tickers", {})

    stale = [t for t in tickers
             if args.force or not is_fresh(entries.get(t), window_start, window_end,
                                           args.max_age)]
    cached = [t for t in tickers if t not in stale]

    print(f"window {window_start} -> {window_end} ({args.days} calendar days)")
    print(f"{len(tickers)} tickers: {len(cached)} cached, {len(stale)} to fetch")
    if stale:
        print(f"  fetching: {', '.join(stale)}")
        print(f"  about {len(stale) * SECONDS_BETWEEN_CALLS / 60:.1f} minutes "
              f"at {SECONDS_BETWEEN_CALLS:.0f}s between calls")

    if args.dry_run:
        if stale:
            print("\ndry run: NOT fetching. Tickers without cached bars are excluded below.")
        missing = [t for t in stale if not entries.get(t, {}).get("closes")]
        tickers = [t for t in tickers if t not in missing]
    elif stale:
        if not args.yes:
            if not sys.stdin.isatty():
                raise CalibrationError("pass --yes to confirm (nothing is reading stdin)")
            if input(f"Fetch {len(stale)} tickers and rewrite seeds.py? [y/N] ").strip(
            ).lower() not in ("y", "yes"):
                return 1
        key = api_key()
        for index, ticker in enumerate(stale):
            if index:
                time.sleep(SECONDS_BETWEEN_CALLS)
            closes = fetch_bars(ticker, window_start, window_end, key)
            entries[ticker] = {
                "start": window_start, "end": window_end,
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                "closes": closes,
            }
            print(f"  {ticker:<8}{len(closes):>5} bars   {closes[-1][1]:>12,.2f}")
        save_cache(cache)
        print(f"cached -> {BARS_CACHE}")

    # ---- measure ----
    series = {t: {day: close for day, close in entries[t]["closes"]}
              for t in tickers if entries.get(t, {}).get("closes")}
    if len(series) < 2:
        raise CalibrationError("need at least two tickers with cached bars")

    days, aligned = align(series)
    if len(days) < MIN_BARS:
        raise CalibrationError(
            f"only {len(days)} trading days common to all {len(series)} tickers, "
            f"below the {MIN_BARS} minimum - a sigma from that little evidence would look "
            f"confident and mean nothing. Drop the short-history names or shorten --days."
        )

    calendar_days = (date.fromisoformat(window_end) - date.fromisoformat(window_start)).days
    measured = {t: measure(t, closes, calendar_days) for t, closes in aligned.items()}
    returns = {t: log_returns(closes) for t, closes in aligned.items()}

    names = sorted(measured)
    correlations = {
        (a, b): pearson(returns[a], returns[b])
        for i, a in enumerate(names) for b in names[i + 1:]
    }

    # ---- report ----
    from app.market.seeds import TICKER_PARAMS as previous_params
    previous = {t: dict(p) for t, p in previous_params.items()}

    print(f"\n  {'TICKER':<8}{'SIGMA':>8}{'MU REAL':>10}{'MU':>7}{'CAGR':>10}"
          f"{'LAST':>12}   was")
    for ticker in names:
        m = measured[ticker]
        before = previous.get(ticker)
        was = (f"sigma {before['sigma']:.3f} mu {before['mu']:.2f}" if before else "NEW")
        print(f"  {ticker:<8}{m.sigma:>8.3f}{m.mu_realised:>10.3f}{m.mu:>7.2f}"
              f"{m.cagr * 100:>9.1f}%{m.last_close:>12,.2f}   {was}")
    print(f"\n  {len(days)} common trading days, {len(correlations)} measured pairs")

    strongest = sorted(correlations.items(), key=lambda kv: -kv[1])[:5]
    print("  strongest correlations: " + ", ".join(
        f"{a}/{b} {v:.2f}" for (a, b), v in strongest))

    if args.dry_run:
        print("\ndry run: seeds.py not written")
        return 0

    window = {"start": window_start, "end": window_end,
              "trading_days": len(days), "pulled": date.today().isoformat()}
    SEEDS_FILE.write_text(render_seeds(measured, correlations, window, previous),
                          encoding="utf-8")
    print(f"\nwrote {SEEDS_FILE}")
    print("Restart the app for the new parameters to take effect - they load at import.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalibrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
