import { expect, type Page, type APIRequestContext } from "@playwright/test";

/**
 * Back to $10,000, the seed watchlist and no history.
 *
 * Every spec calls this first. `/api/portfolio/reset` exists precisely for this: without it
 * the fresh-start scenario passes once and then fails on every subsequent run against a
 * persisted Docker volume (PLAN.md §8).
 */
export async function resetAccount(request: APIRequestContext): Promise<void> {
  const response = await request.post("/api/portfolio/reset");
  expect(response.ok(), "reset should succeed").toBeTruthy();
}

/** Currency text -> number. "$9,127.55" -> 9127.55, "—" -> NaN. */
export function parseMoney(text: string | null): number {
  if (!text) return NaN;
  return Number.parseFloat(text.replace(/[$,+]/g, ""));
}

export async function moneyAt(page: Page, testId: string): Promise<number> {
  return parseMoney(await page.getByTestId(testId).textContent());
}

/** Waits for the SSE stream to deliver a first frame, which is when prices appear. */
export async function waitForStream(page: Page): Promise<void> {
  await expect(page.getByTestId("connection-label")).toHaveText("LIVE FEED");
}

/**
 * Trades through the UI trade bar. The inputs are controlled, so `fill` alone is enough —
 * but the ticker field is also driven by watchlist selection, hence the explicit clear.
 */
export async function tradeViaBar(
  page: Page,
  ticker: string,
  quantity: number,
  side: "buy" | "sell",
): Promise<void> {
  await page.getByTestId("trade-ticker").fill(ticker);
  await page.getByTestId("trade-quantity").fill(String(quantity));
  await page.getByTestId(side === "buy" ? "trade-buy" : "trade-sell").click();
}

/**
 * Puts the account into an exact portfolio via `POST /api/session`.
 *
 * Deliberately NOT a sequence of trades. Trades fill at a price that ticks every 500ms, so
 * a "40% MRVL" book built by trading is 40% ± drift and its cost basis is whatever the tape
 * happened to print — neither reproducible nor exactly the weights the spec asserts on. The
 * session import writes quantities and average costs directly, in one call, so the book is
 * the book the test asked for. See REBALANCE_TEST_HARNESS.md §6.
 *
 * `targets` are weights of the invested sleeve; they need not sum to 1 (they are used as
 * given, against `startingCash * invest`).
 */
export async function seedPortfolio(
  request: APIRequestContext,
  targets: Record<string, number>,
  options: { invest?: number; startingCash?: number } = {},
): Promise<void> {
  const invest = options.invest ?? 0.95;
  const startingCash = options.startingCash ?? 10_000;

  const watchlist = await (await request.get("/api/watchlist")).json();
  const priced = new Map<string, number>(
    watchlist.tickers
      .filter((entry: { priced: boolean }) => entry.priced)
      .map((entry: { ticker: string; price: number }) => [entry.ticker, entry.price]),
  );

  const budget = startingCash * invest;
  const positions = Object.entries(targets).map(([ticker, weight]) => {
    const price = priced.get(ticker);
    if (!price) throw new Error(`${ticker} has no price yet — cannot seed it`);
    return { ticker, quantity: (budget * weight) / price, avg_cost: price };
  });
  const spent = positions.reduce((sum, p) => sum + p.quantity * p.avg_cost, 0);

  const response = await request.post("/api/session", {
    data: {
      version: 1,
      cash_balance: startingCash - spent,
      positions,
      watchlist: watchlist.tickers.map((entry: { ticker: string }) => entry.ticker),
    },
  });
  expect(response.ok(), `seeding failed: ${await response.text()}`).toBeTruthy();
}

/** Equal DOLLAR weight across the given tickers — which is not equal RISK weight. */
export function equalWeights(tickers: string[]): Record<string, number> {
  return Object.fromEntries(tickers.map((ticker) => [ticker, 1 / tickers.length]));
}

export async function openAnalytics(page: Page, tab: "risk" | "rebalance"): Promise<void> {
  await page.getByTestId(tab === "risk" ? "open-risk" : "open-rebalance").click();
  await expect(page.getByTestId("analytics-panel")).toBeVisible();
  await expect(
    page.getByTestId(tab === "risk" ? "risk-report" : "rebalance-preview"),
  ).toBeVisible();
}

/** "63.2%" -> 63.2, "1.23x" -> 1.23, "—" -> NaN. */
export function parseNumeric(text: string | null): number {
  if (!text) return NaN;
  return Number.parseFloat(text.replace(/[%x$,+]/g, ""));
}

export async function numericAt(page: Page, testId: string): Promise<number> {
  return parseNumeric(await page.getByTestId(testId).textContent());
}
