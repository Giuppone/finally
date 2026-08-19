"""Turn the dated CEDEAR ledger into a daily USD equity curve.

STDLIB ONLY - see the note at the top of `ledger.py`.

The whole problem is that the ledger counts CEDEARs, not shares. A CEDEAR is a fractional
claim on a US share at a ratio that differs per stock, priced in pesos or in dollars depending
on how the trade was routed. None of those ratios are in the file, and none of them are in any
file this project has.

They do not need to be. Every ratio is *measurable* from the ledger itself:

    a CEDEAR priced in USD is worth   us_close / ratio
    so                       ratio =  us_close / cedear_price_usd

and a peso price becomes a dollar price through the exchange rate `fx.py` measures from the
bond conversion rows. Take the median across every trade in a ticker and the intraday
fill-versus-close noise averages out - on the real ledger the per-ticker spread is under 5%
for every name with more than two observations.

That leaves four things the ledger cannot tell us, each handled explicitly below:

  1. what the account held before the ledger starts  -> back-solved in `ledger.py`
  2. the ratio for a name held but never traded      -> derived from the holdings snapshot
  3. instruments with no US listing at all           -> carried at cost, never priced
  4. how much cash the account started with          -> the least that never implies borrowing
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Sequence

from . import fx
from .bars import Bars
from .ledger import BUY, LedgerDocument, LedgerRow

# Below this a position is dust: a fully-sold name whose walked quantity lands a few
# floating-point ulps off zero. `SessionPosition.quantity` is `gt=0`, so these must be dropped
# rather than sent as 0.0 - which would 422 the whole session import.
DUST = 1e-9


@dataclass(frozen=True, slots=True)
class DayPoint:
    day: date
    total_value: float
    positions_value: float
    carry_value: float
    cash_balance: float


@dataclass
class Reconstruction:
    """Everything the routes and the session loader need, computed once."""

    points: list[DayPoint] = field(default_factory=list)
    positions: dict[str, float] = field(default_factory=dict)     # terminal SHARE equivalents
    cost_basis: dict[str, float] = field(default_factory=dict)    # USD per share equivalent
    ratios: dict[str, float] = field(default_factory=dict)
    priced: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    fx_points: list[tuple[str, float]] = field(default_factory=list)
    opening_cash: float = 0.0
    opening_carry: float = 0.0
    cash_balance: float = 0.0
    warnings: list[str] = field(default_factory=list)
    as_of: date | None = None
    ratio_sources: dict[str, str] = field(default_factory=dict)
    # Per priced ticker: the user's actual trades in SHARE-equivalent terms, for the chart's
    # buy/sell markers. Without these, a name bought days ago shows months of prior market
    # price and reads as a position held all along.
    events: dict[str, list[dict]] = field(default_factory=dict)
    # First day the user held a position in the ticker: the window start for an opening
    # position, else the date of the first buy.
    held_since: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return len(self.points) >= 2

    @property
    def base_value(self) -> float:
        return self.points[0].total_value if self.points else 0.0


def derive_ratios(
    rows: Sequence[LedgerRow],
    bars: Bars,
    rate_on: Callable[[str], float],
    document: LedgerDocument | None = None,
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    """CEDEARs per US share, per ticker.

    The median, not the mean: a single trade filled at an intraday extreme would drag a mean
    noticeably, and one bad ratio silently rescales that entire position for the whole curve.

    The snapshot fallback is not a nicety. A ticker held throughout the window but never traded
    inside it - GOOGL, on the real file - has no trade to measure against. Without the fallback
    it drops out of the priced set entirely and roughly 4% of the book vanishes from the curve
    with no error anywhere.
    """
    samples: dict[str, list[float]] = {}
    warnings: list[str] = []

    for row in rows:
        close = bars.close_on(row.ticker, date.fromisoformat(row.date))
        if close is None or row.price <= 0:
            continue
        samples.setdefault(row.ticker, []).append(close / fx.price_usd(row, rate_on))

    ratios = {ticker: statistics.median(values) for ticker, values in samples.items()}
    sources = {ticker: "ledger" for ticker in ratios}

    if document and document.snapshot and document.snapshot_date:
        when = date.fromisoformat(document.snapshot_date)
        rate = rate_on(document.snapshot_date)
        for ticker, holding in document.snapshot.items():
            if ticker in ratios or holding.price_ars <= 0:
                continue
            close = bars.close_on(ticker, when)
            if close is None:
                continue
            ratios[ticker] = close / (holding.price_ars / rate)
            sources[ticker] = "snapshot"

    for ticker, values in samples.items():
        if len(values) < 2:
            continue
        spread = (max(values) - min(values)) / ratios[ticker]
        if spread > 0.25:
            warnings.append(
                f"{ticker}: CEDEAR ratio observations spread {spread * 100:.0f}% "
                f"({min(values):.2f}..{max(values):.2f}) - the median {ratios[ticker]:.2f} may "
                f"be unreliable"
            )
    return ratios, sources, warnings


def calendar(bars: Bars, ledger_days: Iterable[date], start: date, end: date) -> list[date]:
    """Trading days UNION ledger dates, clipped to [start, end].

    The union is load-bearing and it cost a real bug to find. 2026-01-19 was Martin Luther
    King Jr Day: the US market was shut, so it is not a trading day, but the Argentine market
    was open and the ledger has two sales on it. Iterating over trading days alone silently
    dropped both, and the end-state reconciliation then failed on exactly those two tickers -
    the only symptom, and only because the check existed.
    """
    days = {day for day in bars.days if start <= day <= end}
    days.update(day for day in ledger_days if start <= day <= end)
    if not days:
        days = set(fx.business_days(start, end))
    return sorted(days)


def _price_positions(
    shares: dict[str, float], bars: Bars, when: date, unpriced_days: dict[str, int]
) -> float:
    total = 0.0
    for ticker, quantity in shares.items():
        if abs(quantity) < DUST:
            continue
        close = bars.close_on(ticker, when)
        if close is None:
            unpriced_days[ticker] = unpriced_days.get(ticker, 0) + 1
            continue
        total += quantity * close
    return total


def build(document: LedgerDocument, bars: Bars) -> Reconstruction:
    """Walk the ledger day by day and value the book at each day's US closes."""
    result = Reconstruction(warnings=list(document.warnings), as_of=bars.as_of)
    rows = document.rows
    if not rows:
        result.warnings.append("the ledger has no buy or sell rows - nothing to reconstruct")
        return result

    # 1. The exchange rate, and the rows that only existed to establish it.
    observations, consumed = fx.observations(rows)
    if not observations:
        result.warnings.append(
            "no same-day currency-conversion pairs in the ledger; peso rows cannot be "
            "converted and the reconstruction is unavailable"
        )
        return result
    rate_on = fx.curve(observations)
    result.fx_points = observations
    effective = [row for index, row in enumerate(rows) if index not in consumed]

    # 2. Ratios, and the split between what can be priced and what must be carried.
    ratios, sources, ratio_warnings = derive_ratios(rows, bars, rate_on, document)
    result.ratios = ratios
    result.ratio_sources = sources
    result.warnings.extend(ratio_warnings)

    universe = set(document.opening) | {row.ticker for row in effective}
    # "Has US daily closes", NOT "has a ratio" and NOT "normalises". GGAL, PAMP and YPFD are
    # Argentine equities whose symbols happen to pass every ticker regex in the project; if
    # they reached `positions` they would land in db.tracked_tickers, and the simulator would
    # invent a GBM price path for a stock that has no US listing.
    priced = {t for t in universe if t in ratios and bars.series(t)}
    carried = sorted(universe - priced)
    result.priced = sorted(priced)
    result.carried = carried

    # Nothing priceable means the bars cache is missing, unreadable, or does not cover this
    # ledger. A curve could still be drawn from the carry bucket alone - and it would be a
    # near-flat line of transacted costs that looks like a portfolio and tracks nothing. That
    # is worse than an empty panel saying so, so this reports unavailable instead.
    if not priced:
        result.warnings.append(
            "no daily closes for any ticker in the ledger - run scripts/calibrate_market.py "
            "to populate backend/calibration/bars.json"
        )
        return result

    # 3. Opening value of the carried names: their first transacted price, in dollars.
    #    Valuing them at zero instead would put a step in the curve on the day they sold,
    #    which reads as a gain the account never made.
    first_price: dict[str, float] = {}
    for row in effective:
        if row.ticker in priced or row.ticker in first_price or row.quantity <= 0 or row.net <= 0:
            continue
        first_price[row.ticker] = fx.to_usd(row, rate_on) / row.quantity

    opening_carry = 0.0
    for ticker in carried:
        quantity = document.opening.get(ticker, 0.0)
        if quantity <= 0:
            continue
        unit = first_price.get(ticker)
        if unit is None:
            result.warnings.append(
                f"{ticker}: held at the start but never transacted and no US closes - "
                f"excluded from the curve entirely"
            )
            continue
        opening_carry += quantity * unit
    result.opening_carry = opening_carry

    # 4. The walk.
    shares = {t: document.opening.get(t, 0.0) / ratios[t] for t in priced
              if document.opening.get(t, 0.0) > 0}
    # Opening lots have no recorded cost, so they are seeded at the first day's close - the
    # only defensible price available. Every later buy blends in at what it actually cost.
    start = date.fromisoformat(min(row.date for row in effective))
    end = bars.as_of or date.fromisoformat(max(row.date for row in effective))
    cost = {t: (bars.close_on(t, start) or 0.0) * q for t, q in shares.items()}

    flows: dict[date, list[LedgerRow]] = {}
    for row in effective:
        flows.setdefault(date.fromisoformat(row.date), []).append(row)

    days = calendar(bars, flows.keys(), start, end)
    if not days:
        result.warnings.append("no overlap between the ledger window and the daily bars")
        return result

    carry, cash = opening_carry, 0.0
    unpriced_days: dict[str, int] = {}
    trail: list[DayPoint] = []
    cash_track: list[float] = []

    for day in days:
        for row in flows.get(day, []):
            amount = fx.to_usd(row, rate_on)
            sign = 1.0 if row.kind == BUY else -1.0
            cash -= sign * amount
            ticker = row.ticker
            if ticker in priced:
                quantity = row.quantity / ratios[ticker]
                held = shares.get(ticker, 0.0)
                if quantity > DUST:
                    result.events.setdefault(ticker, []).append({
                        "date": row.date,
                        "side": row.kind,
                        "shares": round(quantity, 6),
                        # The user's actual fill, converted - NOT that day's close. The gap
                        # between the two is real (intraday timing plus the FX estimate) and
                        # plotting the fill keeps the marker honest against the price line.
                        "price": round(amount / quantity, 4),
                        "usd": round(amount, 2),
                    })
                if row.kind == BUY:
                    shares[ticker] = held + quantity
                    cost[ticker] = cost.get(ticker, 0.0) + amount
                else:
                    # Average cost, matching how portfolio._apply treats a partial sell: the
                    # per-share basis is unchanged, the total is reduced pro rata. FIFO would
                    # be defensible too, but the ledger carries no lot identifiers.
                    if held > DUST:
                        cost[ticker] = cost.get(ticker, 0.0) * max(0.0, 1.0 - min(quantity, held) / held)
                    shares[ticker] = held - quantity
            else:
                carry += sign * amount

        positions_value = _price_positions(shares, bars, day, unpriced_days)
        trail.append(DayPoint(
            day=day,
            total_value=positions_value + carry + cash,
            positions_value=positions_value,
            carry_value=carry,
            cash_balance=cash,
        ))
        cash_track.append(cash)

    # 5. Opening cash: the least that keeps the balance non-negative throughout.
    #    The ledger records trades, not deposits, so the running balance starts at zero and
    #    goes negative the first time a purchase precedes the sale that funded it. Left as-is
    #    it reads as margin the account never had - and `SessionDocument.cash_balance` is
    #    `ge=0`, so the loader would 422 on it. Adding it at day zero rather than on the day
    #    it is first needed keeps the curve free of an artificial step, the same discipline
    #    the carry bucket follows.
    opening_cash = max(0.0, -min(cash_track))
    result.opening_cash = opening_cash
    result.points = [
        DayPoint(
            day=point.day,
            total_value=point.total_value + opening_cash,
            positions_value=point.positions_value,
            carry_value=point.carry_value,
            cash_balance=point.cash_balance + opening_cash,
        )
        for point in trail
    ]
    result.cash_balance = cash + opening_cash
    for ticker in priced:
        if document.opening.get(ticker, 0.0) > 0:
            result.held_since[ticker] = start.isoformat()
        else:
            buys = [e["date"] for e in result.events.get(ticker, []) if e["side"] == BUY]
            if buys:
                result.held_since[ticker] = min(buys)

    result.positions = {t: q for t, q in sorted(shares.items()) if q > DUST}
    result.cost_basis = {
        t: (cost.get(t, 0.0) / q) for t, q in result.positions.items() if q > DUST
    }

    for ticker, count in sorted(unpriced_days.items()):
        result.warnings.append(
            f"{ticker}: held on {count} day(s) with no daily close available - valued at 0 there"
        )
    return result


def reconcile(result: Reconstruction, document: LedgerDocument) -> list[str]:
    """Does the walked book end where the broker's holdings export says it should?

    This is the single most valuable check in the feature. Every step - the exchange rate, the
    ratios, the back-solved openings, the calendar - feeds the terminal position, so if all of
    them are right this comes out exact, and if any one is wrong this is usually the only place
    it shows. It found both bugs that survived the first implementation.
    """
    problems: list[str] = []
    for ticker in result.priced:
        expected = document.snapshot.get(ticker)
        want = (expected.quantity / result.ratios[ticker]) if expected else 0.0
        got = result.positions.get(ticker, 0.0)
        if abs(want - got) > 1e-6:
            problems.append(
                f"{ticker}: walked to {got:.6f} shares, holdings file implies {want:.6f}"
            )
    return problems
