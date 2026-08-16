import { expect, test } from "@playwright/test";

import { resetAccount, waitForStream } from "./helpers";

test.describe("SSE resilience", () => {
  test.beforeEach(async ({ request }) => {
    await resetAccount(request);
  });

  test("drops to reconnecting when the network goes away, then recovers", async ({
    page,
    context,
  }) => {
    await page.goto("/");
    await waitForStream(page);

    await context.setOffline(true);

    // EventSource fires `error`; the hook distinguishes a first connection ("connecting")
    // from a dropped one, so a user who was live sees RECONNECTING, not a silent freeze.
    await expect(page.getByTestId("connection-dot")).toHaveAttribute(
      "data-state",
      /reconnecting|closed/,
      { timeout: 20_000 },
    );

    await context.setOffline(false);

    // The server sends `retry: 1000`, so the browser reconnects on its own.
    await expect(page.getByTestId("connection-label")).toHaveText("LIVE FEED", {
      timeout: 30_000,
    });
  });

  test("prices resume ticking after a reconnect", async ({ page, context }) => {
    await page.goto("/");
    await waitForStream(page);

    await context.setOffline(true);
    await expect(page.getByTestId("connection-dot")).toHaveAttribute(
      "data-state",
      /reconnecting|closed/,
      { timeout: 20_000 },
    );

    await context.setOffline(false);
    await expect(page.getByTestId("connection-label")).toHaveText("LIVE FEED", {
      timeout: 30_000,
    });

    const priceCell = page.getByTestId("watchlist-row-MU").locator("td").nth(1);
    const settled = await priceCell.textContent();
    await expect(priceCell).not.toHaveText(settled ?? "", { timeout: 15_000 });
  });
});
