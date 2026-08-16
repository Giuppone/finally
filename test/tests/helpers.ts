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
