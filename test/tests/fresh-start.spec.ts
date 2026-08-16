import { expect, test } from "@playwright/test";

import { moneyAt, resetAccount, waitForStream } from "./helpers";

const SEED_WATCHLIST = [
  "ALAB", "MRVL", "MU", "AMD", "INTC", "PLTR", "ANET", "LRCX", "AMAT", "SLV",
];

test.describe("fresh start", () => {
  test.beforeEach(async ({ request }) => {
    await resetAccount(request);
  });

  test("shows the seed watchlist, $10,000 and a live price stream", async ({ page }) => {
    await page.goto("/");
    await waitForStream(page);

    // The ten seed tickers (PLAN.md §7) — INTC and MU as real symbols, not "INTEL"/"Micron".
    for (const ticker of SEED_WATCHLIST) {
      await expect(page.getByTestId(`watchlist-row-${ticker}`)).toBeVisible();
    }

    await expect(page.getByTestId("stat-cash")).toHaveText("$10,000.00");
    await expect(page.getByTestId("stat-total-value")).toHaveText("$10,000.00");
  });

  test("prices actually tick", async ({ page }) => {
    await page.goto("/");
    await waitForStream(page);

    const priceCell = page
      .getByTestId("watchlist-row-MU")
      .locator("td")
      .nth(1);

    await expect(priceCell).not.toHaveText("—");
    const first = await priceCell.textContent();

    // The simulator ticks every 500ms; anything under a couple of seconds is generous.
    await expect(priceCell).not.toHaveText(first ?? "", { timeout: 15_000 });
  });

  test("reports the market data mode rather than claiming live", async ({ page }) => {
    await page.goto("/");
    await waitForStream(page);

    // PLAN.md §6: the badge must name the real mode. The E2E stack runs without a Massive
    // key, so it is SIMULATED — claiming "LIVE" here would be the exact dishonesty the
    // three-mode design exists to prevent.
    await expect(page.getByText("SIMULATED", { exact: true })).toBeVisible();
  });

  test("starts with no positions", async ({ page }) => {
    await page.goto("/");
    await waitForStream(page);

    await expect(page.getByTestId("positions-table")).toContainText("No open positions");
    expect(await moneyAt(page, "stat-cash")).toBeCloseTo(10_000, 2);
  });
});
