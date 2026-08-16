import { expect, test } from "@playwright/test";

import { moneyAt, resetAccount, tradeViaBar, waitForStream } from "./helpers";

test.describe("trading", () => {
  test.beforeEach(async ({ request, page }) => {
    await resetAccount(request);
    await page.goto("/");
    await waitForStream(page);
  });

  test("buying reduces cash and opens a position", async ({ page }) => {
    const cashBefore = await moneyAt(page, "stat-cash");
    expect(cashBefore).toBeCloseTo(10_000, 2);

    await tradeViaBar(page, "MU", 2, "buy");

    await expect(page.getByTestId("trade-notice")).toContainText("Bought");
    await expect(page.getByTestId("position-row-MU")).toBeVisible();

    await expect
      .poll(async () => await moneyAt(page, "stat-cash"), { timeout: 15_000 })
      .toBeLessThan(cashBefore);

    // Nothing is created or destroyed by a fill: cash left the balance and became stock.
    const row = page.getByTestId("position-row-MU");
    await expect(row).toContainText("2");
  });

  test("selling returns cash and closes the position", async ({ page }) => {
    await tradeViaBar(page, "AMD", 3, "buy");
    await expect(page.getByTestId("position-row-AMD")).toBeVisible();
    const cashAfterBuy = await moneyAt(page, "stat-cash");

    await tradeViaBar(page, "AMD", 3, "sell");
    await expect(page.getByTestId("trade-notice")).toContainText("Sold");

    await expect
      .poll(async () => await moneyAt(page, "stat-cash"), { timeout: 15_000 })
      .toBeGreaterThan(cashAfterBuy);

    // Selling the whole holding removes the row rather than leaving ~4e-16 dust shares
    // behind (backend/app/portfolio.py, Review.md B11).
    await expect(page.getByTestId("position-row-AMD")).toHaveCount(0);
  });

  test("a partial sell keeps the position and reduces the quantity", async ({ page }) => {
    await tradeViaBar(page, "INTC", 10, "buy");
    await expect(page.getByTestId("position-row-INTC")).toBeVisible();

    await tradeViaBar(page, "INTC", 4, "sell");
    await expect(page.getByTestId("trade-notice")).toContainText("Sold");

    const row = page.getByTestId("position-row-INTC");
    await expect(row).toBeVisible();
    await expect(row).toContainText("6");
  });

  test("an unaffordable buy is rejected with a reason and changes nothing", async ({ page }) => {
    await tradeViaBar(page, "MU", 100_000, "buy");

    await expect(page.getByTestId("trade-notice")).toContainText(/insufficient cash/i);
    await expect(page.getByTestId("position-row-MU")).toHaveCount(0);
    expect(await moneyAt(page, "stat-cash")).toBeCloseTo(10_000, 2);
  });

  test("selling shares that are not held is rejected", async ({ page }) => {
    await tradeViaBar(page, "PLTR", 5, "sell");
    await expect(page.getByTestId("trade-notice")).toContainText(/insufficient shares/i);
  });

  test("buying an unwatched ticker adds it to the watchlist", async ({ page }) => {
    await expect(page.getByTestId("watchlist-row-NVDA")).toHaveCount(0);

    await tradeViaBar(page, "NVDA", 1, "buy");

    await expect(page.getByTestId("trade-notice")).toContainText("Bought");
    // Without the auto-add the position would have no price to update against.
    await expect(page.getByTestId("watchlist-row-NVDA")).toBeVisible();
  });
});
