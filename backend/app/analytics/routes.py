"""The Risk & Return and Suggest Rebalance endpoints (PORTFOLIO_ANALYTICS.md §6).

Both are read-only computations. Nothing here writes to the database, touches the price
cache or executes a trade - applying a suggestion is a separate, explicit call to
`POST /api/portfolio/rebalance`, because a button labelled "suggest" must not trade.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, portfolio
from ..market import InvalidTicker, MarketDataService, get_service, normalize_ticker
from . import estimates, optimize, rebalance, risk

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

DEFAULT_MAX_WEIGHT = 0.35
DEFAULT_MIN_WEIGHT = 0.01


class Holding(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    # Optional so `POST /rebalance` can be handed a bare list of tickers meaning "these
    # names, at whatever I hold today".
    weight: float | None = Field(None, ge=0)


class Constraints(BaseModel):
    max_weight: float = Field(DEFAULT_MAX_WEIGHT, gt=0, le=1)
    min_weight: float = Field(DEFAULT_MIN_WEIGHT, ge=0, lt=1)
    # None = keep the cash fraction the portfolio has now, rather than silently deploying
    # a deliberate cash buffer into the market.
    cash_target: float | None = Field(None, ge=0, lt=1)


class RiskRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    cash_weight: float | None = Field(None, ge=0, le=1)
    total_value: float | None = Field(None, ge=0)


class RebalanceRequest(BaseModel):
    holdings: list[Holding] | None = None
    objective: str = "min_variance"
    constraints: Constraints = Field(default_factory=Constraints)


def _normalize(raw: str) -> str:
    try:
        return normalize_ticker(raw)
    except InvalidTicker as exc:
        raise HTTPException(400, str(exc)) from exc


def _weights(holdings: list[Holding]) -> tuple[list[str], list[float], list[str]]:
    """Normalise a request's holdings into (tickers, weights summing to <= 1, warnings)."""
    tickers: list[str] = []
    raw: list[float] = []
    seen: set[str] = set()
    for holding in holdings:
        symbol = _normalize(holding.ticker)
        if symbol in seen:
            raise HTTPException(400, f"duplicate holding for {symbol}")
        seen.add(symbol)
        tickers.append(symbol)
        raw.append(holding.weight if holding.weight is not None else 0.0)

    warnings = [f"{t}: no calibrated parameters, using defaults"
                for t in tickers if not estimates.is_known(t)]

    total = sum(raw)
    if total <= 0:
        # No weights supplied at all - read it as "equally, across these names" rather than
        # as a zero-size portfolio, which has no risk to report.
        return tickers, [1.0 / len(tickers)] * len(tickers) if tickers else [], warnings
    if total > 1.0 + 1e-9:
        warnings.append(f"weights summed to {total:.4f}; normalised to 1.00")
        return tickers, [w / total for w in raw], warnings
    return tickers, raw, warnings


async def _live_context(service: MarketDataService) -> dict:
    """Current positions valued at live prices, plus cash. The shared starting point for
    both endpoints' defaults."""
    state = await db.run(lambda conn: portfolio.value_portfolio(conn, service))
    values, prices, quantities, unpriced = {}, {}, {}, []
    for holding in state["positions"]:
        if not holding["priced"]:
            unpriced.append(holding["ticker"])
            continue
        values[holding["ticker"]] = holding["market_value"]
        prices[holding["ticker"]] = holding["price"]
        # Carried alongside the value so a full exit can sell the exact stored quantity
        # rather than value/price, which rounds and leaves a sliver behind.
        quantities[holding["ticker"]] = holding["quantity"]
    return {"state": state, "values": values, "prices": prices,
            "quantities": quantities, "unpriced": unpriced}


@router.post("/risk")
async def post_risk(
    body: RiskRequest,
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Risk, expected return and the per-name risk decomposition for a weight vector."""
    context = await _live_context(service)

    if body.holdings:
        tickers, weights, warnings = _weights(body.holdings)
    else:
        # Default to what the user actually holds, scaled so cash keeps its real weight.
        state = context["state"]
        total = state["total_value"]
        tickers = sorted(context["values"])
        weights = [context["values"][t] / total for t in tickers] if total > 0 else []
        warnings = [f"{t}: no live price, excluded" for t in context["unpriced"]]
        warnings += [f"{t}: no calibrated parameters, using defaults"
                     for t in tickers if not estimates.is_known(t)]

    if not tickers:
        raise HTTPException(400, "no positions to analyse: buy something, or select "
                                 "tickers to model")

    cash_weight = body.cash_weight
    if cash_weight is None:
        cash_weight = max(1.0 - sum(weights), 0.0)

    total_value = body.total_value
    if total_value is None:
        total_value = context["state"]["total_value"]

    stats = risk.portfolio_stats(tickers, weights, cash_weight=cash_weight,
                                 total_value=total_value)

    # The frontier rides along with the Risk tab only: the rebalance tab compares two
    # concrete portfolios, where a curve neither of them sits on is noise.
    if len(tickers) >= 2:
        curve = await risk.frontier_for(tuple(tickers))
        stats["frontier"] = [{"volatility": round(v, 6), "expected_return": round(r, 6)}
                             for v, r in curve]
        # Measured against the risky sleeve renormalised to 1. The frontier is a property of
        # the SELECTION, not of how much cash sits beside it, and a half-cash book compared
        # against it raw would be reported as implausibly far inside a curve it was never on.
        risky = sum(weights)
        if risky > 1e-9:
            scaled = [w / risky for w in weights]
            cov = estimates.covariance(tickers)
            mu = estimates.drifts(tickers)
            stats["frontier_gap"] = optimize.frontier_gap(
                risk.portfolio_volatility(cov, scaled),
                sum(w * m for w, m in zip(scaled, mu)),
                list(curve),
            )

    return stats | {"warnings": warnings}


@router.post("/rebalance")
async def post_rebalance(
    body: RebalanceRequest,
    service: MarketDataService = Depends(get_service),
) -> dict:
    """Suggest target weights and the ordered trades that reach them. Executes nothing."""
    if body.objective not in optimize.OBJECTIVES:
        raise HTTPException(
            400, f"unknown objective {body.objective!r}; expected one of "
                 f"{', '.join(optimize.OBJECTIVES)}"
        )

    context = await _live_context(service)
    state, values, prices = context["state"], context["values"], context["prices"]
    warnings = [f"{t}: no live price, excluded" for t in context["unpriced"]]

    if body.holdings:
        tickers, _, holding_warnings = _weights(body.holdings)
        warnings += holding_warnings
        # A selected ticker with no position yet still needs a price to be traded into.
        for ticker in tickers:
            if ticker not in prices:
                quote = service.quote(ticker) or await service.add_ticker(ticker)
                if quote is None:
                    warnings.append(f"{ticker}: no market data, excluded")
                    continue
                prices[ticker] = quote.price
        tickers = [t for t in tickers if t in prices]
    else:
        tickers = sorted(values)

    if len(tickers) < 2:
        raise HTTPException(
            400, "a rebalance needs at least two priced names; with one there is nothing "
                 "to trade off against"
        )

    cov = estimates.covariance(tickers)
    mu = estimates.drifts(tickers)
    constraints = body.constraints
    try:
        target_weights = optimize.solve(body.objective, mu, cov,
                                        estimates.RISK_FREE_RATE, constraints.max_weight)
        target_weights = optimize.apply_floor(target_weights, constraints.min_weight,
                                              constraints.max_weight)
    except optimize.Infeasible as exc:
        raise HTTPException(400, str(exc)) from exc

    if constraints.max_weight <= 1.0 / len(tickers) + 1e-9:
        warnings.append(
            f"max_weight {constraints.max_weight:.0%} forces equal weights across "
            f"{len(tickers)} names"
        )

    cash = state["cash_balance"]
    total_value = state["total_value"]
    cash_reserve = (total_value * constraints.cash_target
                    if constraints.cash_target is not None else cash)

    targets = dict(zip(tickers, target_weights))
    # Positions outside the selection are still part of the book, so they must appear in the
    # plan - with a target of zero, which is what "rebalance into these names" means.
    current_values = {t: v for t, v in values.items()}
    rows, trades, plan_warnings = rebalance.build_plan(
        targets=targets, current_values=current_values, prices=prices,
        cash=cash, cash_reserve=cash_reserve,
        current_quantities=context["quantities"],
    )
    warnings += plan_warnings

    before_tickers = sorted(current_values)
    before_weights = [current_values[t] / total_value for t in before_tickers] if total_value else []
    after_tickers = sorted(set(targets) | set(current_values))
    invested = max(total_value - cash_reserve, 0.0)
    after_weights = [targets.get(t, 0.0) * invested / total_value if total_value else 0.0
                     for t in after_tickers]

    return {
        "objective": body.objective,
        "constraints": constraints.model_dump(),
        "before": risk.portfolio_stats(
            before_tickers, before_weights,
            cash_weight=max(1.0 - sum(before_weights), 0.0), total_value=total_value,
        ) if before_tickers else None,
        "after": risk.portfolio_stats(
            after_tickers, after_weights,
            cash_weight=max(1.0 - sum(after_weights), 0.0), total_value=total_value,
        ),
        "targets": rows,
        "trades": trades,
        "estimated_cash_after": round(
            cash + sum(t["notional"] if t["side"] == "sell" else -t["notional"]
                       for t in trades), 2),
        "warnings": warnings,
    }
