"""Turn target weights into an ordered, executable trade list.

Four rules, each of which exists because its absence produces a specific failure
(PORTFOLIO_ANALYTICS.md §5):

1. sells before buys - a buy-first ordering fails on insufficient cash for exactly the
   trades that most need to happen
2. drop legs under MIN_TRADE_NOTIONAL - otherwise a 0.3% drift generates ten $2 trades
3. clamp a buy to the cash actually available - prices tick every 500ms, so between the
   quote the plan was built on and the last fill, cash can come up short by cents
4. skip unpriced positions - valuing them at avg_cost is right for the portfolio panel and
   wrong here, where it would look like a zero-volatility asset
"""

from __future__ import annotations

import math

MIN_TRADE_NOTIONAL = 10.0
QUANTITY_DP = 4


def _truncate(quantity: float) -> float:
    scale = 10 ** QUANTITY_DP
    return math.floor(quantity * scale) / scale


def build_plan(
    *,
    targets: dict[str, float],
    current_values: dict[str, float],
    prices: dict[str, float],
    cash: float,
    cash_reserve: float,
    current_quantities: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (target rows, ordered trades, warnings).

    `targets` are weights of the invested sleeve; `cash_reserve` is held back from
    `total_value` before those weights are applied.
    """
    warnings: list[str] = []
    total_value = cash + sum(current_values.values())
    investable = max(total_value - cash_reserve, 0.0)

    universe = sorted(set(targets) | set(current_values))
    rows: list[dict] = []
    for ticker in universe:
        target_weight = targets.get(ticker, 0.0)
        current_value = current_values.get(ticker, 0.0)
        target_value = investable * target_weight
        rows.append({
            "ticker": ticker,
            "current_weight": round(current_value / total_value, 6) if total_value else 0.0,
            "target_weight": round(target_weight, 6),
            "current_value": round(current_value, 2),
            "target_value": round(target_value, 2),
            "delta_value": round(target_value - current_value, 2),
        })

    sells: list[dict] = []
    buys: list[dict] = []
    for row in rows:
        ticker = row["ticker"]
        delta = row["target_value"] - row["current_value"]
        price = prices.get(ticker)
        if price is None or price <= 0:
            if abs(delta) >= MIN_TRADE_NOTIONAL:
                warnings.append(f"{ticker}: no live price, cannot trade it")
            continue
        if abs(delta) < MIN_TRADE_NOTIONAL:
            continue

        held = (current_quantities or {}).get(ticker, 0.0)
        exiting = delta < 0 and row["target_value"] < MIN_TRADE_NOTIONAL and held > 0
        if exiting:
            # Sell the EXACT held quantity, not the truncated delta. Truncation leaves a
            # sliver - 0.4537 held, 0.4536 sold - and 0.0001 shares is above the DUST
            # threshold, so the position survives as a $0.03 phantom row that renders in the
            # table and pins the ticker in the price cache forever (Review.md B11). Passing
            # the stored quantity rather than value/price also avoids the rounding that
            # could otherwise ask to sell a hair more than is owned.
            quantity = held
        else:
            quantity = _truncate(abs(delta) / price)
        if quantity <= 0:
            continue
        leg = {"ticker": ticker, "side": "buy" if delta > 0 else "sell",
               "quantity": quantity, "price": round(price, 4),
               "notional": round(quantity * price, 2), "clamped": False}
        (buys if delta > 0 else sells).append(leg)

    trades = sells + buys

    # Walk the sequence with a running cash balance, exactly as the executor will, and clamp
    # any buy that outruns it. Doing this per-leg rather than only on the last one also
    # covers the case where a sell was dropped as dust and the buys no longer fit.
    running = cash
    executable: list[dict] = []
    for leg in trades:
        if leg["side"] == "sell":
            running += leg["notional"]
            executable.append(leg)
            continue
        if leg["notional"] <= running:
            running -= leg["notional"]
            executable.append(leg)
            continue
        affordable = _truncate(running / leg["price"])
        if affordable * leg["price"] < MIN_TRADE_NOTIONAL:
            warnings.append(f"{leg['ticker']}: dropped, only ${running:,.2f} cash left")
            continue
        leg = leg | {"quantity": affordable,
                     "notional": round(affordable * leg["price"], 2), "clamped": True}
        warnings.append(f"{leg['ticker']}: buy clamped to the ${running:,.2f} available")
        running -= leg["notional"]
        executable.append(leg)

    return rows, executable, warnings


def current_weights(current_values: dict[str, float]) -> dict[str, float]:
    """Weights of the invested sleeve only - cash is handled separately, so these sum to 1
    across the positions rather than to (1 - cash weight)."""
    invested = sum(current_values.values())
    if invested <= 0:
        return {}
    return {ticker: value / invested for ticker, value in current_values.items()}
