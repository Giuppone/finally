// Typed fetch wrappers. Same-origin relative paths only (PLAN.md §10): FastAPI serves this
// export and the API from one port, so there is no base URL and no CORS.

import type {
  ChatMessage,
  ChatResponse,
  DailySeries,
  HoldingInput,
  Portfolio,
  HistoryRange,
  PortfolioCurve,
  PortfolioHistory,
  PricePoint,
  RebalanceObjective,
  RebalancePlan,
  RebalanceResult,
  RebalanceTrade,
  RiskStats,
  Side,
  TradeResult,
  WatchlistResponse,
} from "./types";

/** An API error carrying the backend's structured `detail`, so callers can read `code`. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, fallback: string) {
    super(readableDetail(detail) ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function readableDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const reason = (detail as { reason?: unknown }).reason;
    if (typeof reason === "string") return reason;
    // FastAPI validation errors arrive as a list of {msg, loc, ...}.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === "string") return first.msg;
    }
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json", ...init?.headers } : init?.headers,
  });

  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = (await response.json())?.detail ?? null;
    } catch {
      // A proxy error page or an empty body — fall through to the status text.
    }
    throw new ApiError(response.status, detail, `${response.status} ${response.statusText}`);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---- watchlist --------------------------------------------------------------

export const getWatchlist = () => request<WatchlistResponse>("/api/watchlist");

export const addTicker = (ticker: string) =>
  request<unknown>("/api/watchlist", {
    method: "POST",
    body: JSON.stringify({ ticker }),
  });

export const removeTicker = (ticker: string) =>
  request<void>(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: "DELETE" });

// ---- portfolio --------------------------------------------------------------

export const getPortfolio = () => request<Portfolio>("/api/portfolio");

export const getPortfolioHistory = (limit = 500) =>
  request<PortfolioHistory>(`/api/portfolio/history?limit=${limit}`);

export const trade = (ticker: string, quantity: number, side: Side) =>
  request<{ trade: TradeResult; portfolio: Portfolio }>("/api/portfolio/trade", {
    method: "POST",
    body: JSON.stringify({ ticker, quantity, side }),
  });

export const resetPortfolio = () =>
  request<Portfolio>("/api/portfolio/reset", { method: "POST" });

// ---- prices -----------------------------------------------------------------

export const getHistory = (ticker: string, limit = 1000) =>
  request<{ ticker: string; points: PricePoint[] }>(
    `/api/prices/${encodeURIComponent(ticker)}/history?limit=${limit}`,
  );

/** Bulk form: seeds every sparkline in one round trip instead of N (PLAN.md §8). */
export const getBulkHistory = (tickers: string[], limit = 60) =>
  request<{ series: Record<string, PricePoint[]> }>(
    `/api/prices/history?tickers=${encodeURIComponent(tickers.join(","))}&limit=${limit}`,
  );

// ---- daily history ----------------------------------------------------------
//
// Separate from getPortfolioHistory / getHistory above, matching the backend's separate
// routes. Neither the meaning nor the shape is the same: those serve the live paper account
// and the intraday ring buffer, these serve the reconstructed real book at daily closes.

/** 200 with `available: false` when no ledger has been generated — never a 404. */
export const getPortfolioCurve = (range: HistoryRange = "max") =>
  request<PortfolioCurve>(`/api/history/portfolio?range=${range}`);

export const getDailyPrices = (ticker: string, range: HistoryRange = "max") =>
  request<DailySeries>(
    `/api/history/prices/${encodeURIComponent(ticker)}?range=${range}`,
  );

// ---- chat -------------------------------------------------------------------

export const sendChat = (message: string) =>
  request<ChatResponse>("/api/chat", {
    method: "POST",
    body: JSON.stringify({ message }),
  });

export const getChatHistory = () =>
  request<{ messages: ChatMessage[]; mock: boolean }>("/api/chat/history");

// ---- analytics --------------------------------------------------------------

/** Risk and expected return for a weight vector. Empty holdings = the live portfolio. */
export const postRisk = (holdings: HoldingInput[] = []) =>
  request<RiskStats>("/api/analytics/risk", {
    method: "POST",
    body: JSON.stringify({ holdings }),
  });

/** Suggests target weights and the trades to reach them. Executes nothing. */
export const postRebalance = (
  objective: RebalanceObjective,
  holdings: HoldingInput[] | null,
  maxWeight: number,
) =>
  request<RebalancePlan>("/api/analytics/rebalance", {
    method: "POST",
    body: JSON.stringify({
      objective,
      holdings,
      constraints: { max_weight: maxWeight },
    }),
  });

/** Executes a suggested plan — the whole batch under one hold of the trade lock. */
export const applyRebalance = (trades: RebalanceTrade[]) =>
  request<RebalanceResult>("/api/portfolio/rebalance", {
    method: "POST",
    body: JSON.stringify({
      trades: trades.map(({ ticker, side, quantity }) => ({ ticker, side, quantity })),
    }),
  });
