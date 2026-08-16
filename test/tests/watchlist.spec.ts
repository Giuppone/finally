import { expect, test } from "@playwright/test";

import { resetAccount, waitForStream } from "./helpers";

test.describe("watchlist", () => {
  test.beforeEach(async ({ request, page }) => {
    await resetAccount(request);
    await page.goto("/");
    await waitForStream(page);
  });

  test("adds a ticker and prices it", async ({ page }) => {
    await expect(page.getByTestId("watchlist-row-NVDA")).toHaveCount(0);

    await page.getByLabel("Add ticker to watchlist").fill("nvda");
    await page.getByRole("button", { name: "Add", exact: true }).click();

    const row = page.getByTestId("watchlist-row-NVDA");
    await expect(row).toBeVisible();

    // `POST /api/watchlist` prices the ticker before it returns, so the row must never sit
    // at "—" waiting for the next poll.
    await expect(row.locator("td").nth(1)).not.toHaveText("—");
  });

  test("removes a ticker", async ({ page }) => {
    const row = page.getByTestId("watchlist-row-SLV");
    await expect(row).toBeVisible();

    await row.hover();
    await row.getByRole("button", { name: "Remove SLV" }).click();

    await expect(page.getByTestId("watchlist-row-SLV")).toHaveCount(0);
  });

  test("rejects a malformed ticker without breaking the list", async ({ page }) => {
    await page.getByLabel("Add ticker to watchlist").fill("not a ticker");
    await page.getByRole("button", { name: "Add", exact: true }).click();

    await expect(page.locator("text=/invalid|unknown|not a symbol/i").first()).toBeVisible();
    // The ten seed rows are still there.
    await expect(page.getByTestId("watchlist-row-MU")).toBeVisible();
  });

  test("selecting a ticker drives the main chart", async ({ page }) => {
    await page.getByTestId("watchlist-row-AMD").click();
    await expect(page.getByText("AMD", { exact: true }).first()).toBeVisible();
    // The chart seeds from /api/prices/{ticker}/history, so it must not sit empty.
    await expect(page.locator("text=Select a ticker from the watchlist")).toHaveCount(0);
  });
});
