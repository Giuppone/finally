// Re-values the portfolio against the live stream.
//
// `GET /api/portfolio` is a point-in-time snapshot, but prices arrive every 500ms. Without
// this the header's "total value" would only move when something refetched it, which is
// exactly the number PLAN.md §10 asks to update live. The arithmetic deliberately mirrors
// backend/app/portfolio.py `value_portfolio` — including its rule that an unpriced position
// is valued at avg_cost rather than 0, since 0 renders as -100% P&L.

import type { Portfolio, Position, Quote } from "./types";

export interface LivePosition extends Position {
  live_price: number;
  live_market_value: number;
  live_unrealized_pnl: number;
  live_unrealized_pnl_pct: number;
  live_weight: number;
  /** True when a live quote backs this row; false means the number is a fallback. */
  live_priced: boolean;
}

export interface LivePortfolio {
  positions: LivePosition[];
  cash_balance: number;
  positions_value: number;
  total_value: number;
  unrealized_pnl: number;
  starting_cash: number;
  total_return: number;
  total_return_pct: number;
  all_priced: boolean;
}

export function derivePortfolio(
  portfolio: Portfolio | null,
  quotes: Record<string, Quote>,
): LivePortfolio | null {
  if (!portfolio) return null;

  const positions: LivePosition[] = portfolio.positions.map((position) => {
    const quote = quotes[position.ticker];
    const priced = quote != null || position.priced;
    const price = quote?.price ?? position.price;
    const marketValue = position.quantity * price;
    const costBasis = position.quantity * position.avg_cost;
    const pnl = marketValue - costBasis;
    return {
      ...position,
      live_priced: priced,
      live_price: price,
      live_market_value: marketValue,
      live_unrealized_pnl: pnl,
      live_unrealized_pnl_pct: costBasis ? (pnl / costBasis) * 100 : 0,
      live_weight: 0, // filled below, once the denominator is known
    };
  });

  const positionsValue = positions.reduce((sum, p) => sum + p.live_market_value, 0);
  for (const position of positions) {
    position.live_weight = positionsValue ? position.live_market_value / positionsValue : 0;
  }

  const totalValue = portfolio.cash_balance + positionsValue;
  return {
    positions,
    cash_balance: portfolio.cash_balance,
    positions_value: positionsValue,
    total_value: totalValue,
    unrealized_pnl: positions.reduce((sum, p) => sum + p.live_unrealized_pnl, 0),
    starting_cash: portfolio.starting_cash,
    total_return: totalValue - portfolio.starting_cash,
    total_return_pct:
      ((totalValue - portfolio.starting_cash) / portfolio.starting_cash) * 100,
    all_priced: positions.every((p) => p.live_priced),
  };
}
