"""The ARS/USD rate, measured from the ledger rather than supplied to it.

STDLIB ONLY - see the note at the top of `ledger.py`.

An Argentine investor converts currency by buying a sovereign bond in one currency and selling
the same bond in the other on the same day ("rulo", or dollar-MEP). That leaves a distinctive
fingerprint in the ledger: two rows, same date, same ticker, same quantity, opposite sides,
different currencies. The ratio of their `Neto` is the exact rate that trade executed at.

This is worth more than any external rate series would be. It is the rate THIS account
actually transacted at, on the days it transacted, with the spread already inside it - and it
costs nothing, because the rows are already in the file.

The same two rows must then be removed from the position walk. They moved currency, not
holdings: counting them as a real buy and a real sell of a bond would net to zero units but
would double-count the cash.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Sequence

from .ledger import LedgerRow


def observations(rows: Sequence[LedgerRow]) -> tuple[list[tuple[str, float]], frozenset[int]]:
    """Find the currency-conversion pairs.

    Returns `(sorted [(date, ars_per_usd)], indices of the rows consumed)`.

    All four conditions are required. Dropping any one of them starts matching real trades:
    two same-day sales of the same size in the same currency are two sales, not a conversion.
    """
    by_day: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        by_day.setdefault(row.date, []).append(index)

    rates: dict[str, float] = {}
    consumed: set[int] = set()

    for day, indices in by_day.items():
        for position, left in enumerate(indices):
            if left in consumed:
                continue
            for right in indices[position + 1:]:
                if right in consumed:
                    continue
                a, b = rows[left], rows[right]
                if a.ticker != b.ticker or a.quantity != b.quantity:
                    continue
                if a.currency == b.currency or a.kind == b.kind:
                    continue
                usd, ars = (a, b) if a.currency == "USD" else (b, a)
                if usd.net <= 0 or ars.net <= 0:
                    continue
                rates[day] = ars.net / usd.net
                consumed.update((left, right))
                break

    return sorted(rates.items()), frozenset(consumed)


def curve(points: Sequence[tuple[str, float]]) -> Callable[[str], float]:
    """Linear interpolation between observations, flat outside them.

    Flat rather than extrapolated on purpose. The peso depreciates, so a linear extrapolation
    off the last two points would keep depreciating it forever and quietly inflate every USD
    figure past the end of the data. A flat carry is visibly conservative and wrong in a
    direction that is easy to reason about.
    """
    if not points:
        raise ValueError(
            "no ARS/USD observations in the ledger - every peso-denominated row would be "
            "unconvertible. The rate is derived from same-day bond conversion pairs; a ledger "
            "with none of those needs a rate supplied another way."
        )

    days = [date.fromisoformat(day) for day, _ in points]
    rates = [rate for _, rate in points]

    def rate_on(iso: str) -> float:
        when = date.fromisoformat(iso)
        if when <= days[0]:
            return rates[0]
        if when >= days[-1]:
            return rates[-1]
        for index in range(len(days) - 1):
            lo, hi = days[index], days[index + 1]
            if lo <= when <= hi:
                span = (hi - lo).days
                if span == 0:
                    return rates[index]
                offset = (when - lo).days
                return rates[index] + (rates[index + 1] - rates[index]) * offset / span
        return rates[-1]

    return rate_on


def to_usd(row: LedgerRow, rate_on: Callable[[str], float]) -> float:
    """The row's total consideration in dollars, fees included."""
    return row.net if row.currency == "USD" else row.net / rate_on(row.date)


def price_usd(row: LedgerRow, rate_on: Callable[[str], float]) -> float:
    """The per-CEDEAR price in dollars."""
    return row.price if row.currency == "USD" else row.price / rate_on(row.date)


def span(points: Sequence[tuple[str, float]]) -> tuple[float, float]:
    """First and last observed rate, for the response metadata."""
    if not points:
        return (0.0, 0.0)
    return (points[0][1], points[-1][1])


def business_days(start: date, end: date) -> list[date]:
    """Weekdays in [start, end] - a fallback calendar when no bar data covers the range."""
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
