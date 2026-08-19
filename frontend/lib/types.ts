// Wire types. These mirror the backend exactly, including its snake_case: the API is
// snake_case by convention (backend/app/routes.py) and mapping it at the boundary would
// mean maintaining two names for every field. Timestamps follow the backend's split —
// epoch milliseconds on market data, ISO-8601 UTC everywhere else (backend/app/clock.py).

export type Mode = "simulated" | "anchored" | "live";
export type Direction = "up" | "down" | "flat";
export type Side = "buy" | "sell";

export interface Quote {
  ticker: string;
  price: number;
  prev_price: number;
  /** Session anchor. Daily change is measured from here, never from prev_price. */
  open_price: number;
  change: number;
  change_pct: number;
  direction: Direction;
  /** Epoch milliseconds. */
  ts: number;
}

/** An unpriced entry carries no price keys at all — render "—", never 0. */
export type WatchlistEntry = {
  ticker: string;
  added_at: string;
  priced: boolean;
} & Partial<Quote>;

export interface WatchlistResponse {
  tickers: WatchlistEntry[];
  mode: Mode;
}

export interface Position {
  ticker: string;
  quantity: number;
  avg_cost: number;
  price: number;
  priced: boolean;
  market_value: number;
  cost_basis: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  weight: number;
  updated_at: string;
}

export interface Portfolio {
  cash_balance: number;
  positions: Position[];
  positions_value: number;
  total_value: number;
  unrealized_pnl: number;
  starting_cash: number;
  total_return: number;
  total_return_pct: number;
  all_priced: boolean;
}

export interface PricePoint {
  /** Epoch milliseconds. */
  ts: number;
  price: number;
}

export interface SnapshotPoint {
  /** ISO-8601 UTC. */
  recorded_at: string;
  total_value: number;
}

export interface PortfolioHistory {
  points: SnapshotPoint[];
  starting_cash: number;
}

// ---- daily history (backend/app/history) ------------------------------------
//
// A different series from PortfolioHistory above, not a longer one. That is the live
// $10,000 paper account sampled every 30s; this is the real brokerage book reconstructed
// from a dated ledger and valued at daily US closes. They share an axis and nothing else.

export type HistoryRange = "1m" | "3m" | "6m" | "ytd" | "max";

export interface DailyPoint {
  /** ISO-8601 calendar date. A daily close has no time of day. */
  date: string;
  /** Epoch milliseconds at 00:00 UTC, so one chart accessor reads this and PricePoint. */
  ts: number;
  close: number;
}

export interface CurvePoint {
  date: string;
  ts: number;
  total_value: number;
  /** Rebased server-side to the first point of the FILTERED window. */
  return_pct: number;
  positions_value: number;
  carry_value: number;
  cash_balance: number;
}

export interface HistoryMeta {
  bars_through: string | null;
  bars_from: string | null;
  bars_fetched_at: string | null;
  ranges: HistoryRange[];
  tickers_priced?: number;
  tickers_carried?: number;
  carried?: string[];
  fx_observations?: number;
  fx_start?: number;
  fx_end?: number;
  opening_cash?: number;
  opening_carry?: number;
}

export interface PortfolioCurve {
  /** False on a build with no generated ledger — hide the non-live ranges, do not error. */
  available: boolean;
  currency: string;
  range: HistoryRange;
  start_date: string | null;
  end_date: string | null;
  as_of: string | null;
  base_value: number;
  points: CurvePoint[];
  warnings: string[];
  meta: HistoryMeta;
}

/** One of the user's own trades, in US-share-equivalent terms, for chart markers. */
export interface TradeMark {
  date: string;
  ts: number;
  side: Side;
  shares: number;
  /** The actual fill converted to USD per share — not that day's close. */
  price: number;
  usd: number;
}

export interface DailySeries {
  ticker: string;
  range: HistoryRange;
  start_date: string | null;
  end_date: string | null;
  points: DailyPoint[];
  /** The user's trades inside the window; empty when they never traded this name. */
  trades: TradeMark[];
  /** First day the user held the name, or null if they never did. */
  held_since: string | null;
}

export interface TradeResult {
  status: "filled" | "rejected";
  ticker: string;
  side: Side;
  quantity: number;
  watchlist_added: boolean;
  fill_price?: number;
  total?: number;
  cash_balance?: number;
  executed_at?: string;
  reason?: string;
  code?: string;
}

export interface ChatAction {
  kind: "trade" | "watchlist";
  status: "executed" | "rejected" | "skipped";
  ticker: string;
  detail: string;
  side?: string;
  quantity?: number;
  fill_price?: number;
  total?: number;
  action?: string;
  code?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  actions: ChatAction[];
  created_at: string;
}

export interface ChatResponse {
  message: ChatMessage;
  /** Present only when an action actually changed the portfolio. */
  portfolio: Portfolio | null;
}

// ---- SSE frames -------------------------------------------------------------
// Both arrive as UNNAMED `data:` events discriminated by `type` — the backend never sets
// an event name, so addEventListener("prices") would receive nothing. Use onmessage.

export interface HelloFrame {
  type: "hello";
  mode: Mode;
  tick_ms: number;
  poll_interval_s: number | null;
  session_date: string;
  healthy: boolean;
  quotes: Quote[];
}

export interface PricesFrame {
  type: "prices";
  seq: number;
  healthy: boolean;
  quotes: Quote[];
}

export type StreamFrame = HelloFrame | PricesFrame;

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

// ---- analytics (backend/app/analytics) --------------------------------------

export interface RiskPosition {
  ticker: string;
  weight: number;
  /** Annualised, decimal. The simulator's damped drift — see expected_return_basis. */
  expected_return: number;
  volatility: number;
  marginal_risk: number;
  /** Sums to `volatility` across positions (Euler decomposition). */
  risk_contribution: number;
  /** Sums to 1 across positions. */
  risk_share: number;
  /** False when the ticker falls back to generic parameters rather than measured ones. */
  calibrated: boolean;
  /**
   * Realised compound annual growth over the calibration window. Display only — it sits
   * beside the damped `expected_return` so the damping is auditable. Runs to several
   * hundred percent on this basket. Null when never measured.
   */
  cagr: number | null;
}

/** The window every sigma, mu and correlation was measured over. */
export interface CalibrationWindow {
  start: string;
  end: string;
  trading_days: number;
  pulled: string;
}

export interface RiskStats {
  /** Empty on the rebalance tab and for a single-name selection. */
  frontier: FrontierPoint[];
  frontier_gap: FrontierGap | Record<string, never>;
  expected_return: number;
  volatility: number;
  /** null for an all-cash book — there is no risk to be compensated for. */
  sharpe: number | null;
  var_95_1d_parametric: number | null;
  diversification_ratio: number | null;
  effective_n: number;
  cash_weight: number;
  risk_free_rate: number;
  /** Render this beside any expected return. It is not a forecast. */
  expected_return_basis: string;
  calibration: CalibrationWindow;
  positions: RiskPosition[];
  correlations: { tickers: string[]; matrix: number[][] };
  warnings: string[];
}

export type RebalanceObjective =
  | "min_variance"
  | "risk_parity"
  | "max_sharpe"
  | "equal_weight";

export interface RebalanceTarget {
  ticker: string;
  current_weight: number;
  target_weight: number;
  current_value: number;
  target_value: number;
  delta_value: number;
}

export interface RebalanceTrade {
  ticker: string;
  side: Side;
  quantity: number;
  price: number;
  notional: number;
  /** True when the leg was shrunk to the cash actually available. */
  clamped: boolean;
}

export interface RebalancePlan {
  objective: RebalanceObjective;
  constraints: { max_weight: number; min_weight: number; cash_target: number | null };
  before: RiskStats | null;
  after: RiskStats;
  targets: RebalanceTarget[];
  /** Already ordered for execution: sells first, then buys. */
  trades: RebalanceTrade[];
  estimated_cash_after: number;
  warnings: string[];
}

export interface RebalanceResult {
  trades: TradeResult[];
  filled: number;
  rejected: number;
  portfolio: Portfolio;
}

export interface HoldingInput {
  ticker: string;
  weight?: number;
}

export interface FrontierPoint {
  volatility: number;
  expected_return: number;
}

/** How far inside the efficient frontier a portfolio sits, read both ways. */
export interface FrontierGap {
  volatility_at_same_return: number;
  /** Risk you could shed without giving up any expected return. */
  avoidable_volatility: number;
  return_at_same_volatility: number;
  /** Return you could add without taking on any more risk. */
  forgone_return: number;
}
