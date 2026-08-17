import { expect, test, type Page } from "@playwright/test";

import { resetAccount, waitForStream } from "./helpers";

/**
 * Price-stream resilience.
 *
 * These specs cut the stream by intercepting its route, not with `context.setOffline()`.
 * Offline emulation cannot do the job here: under compose the browser reaches the app over
 * loopback (see docker-compose.test.yml for why it has to), and Chromium's offline emulation
 * neither applies to loopback nor severs an already-established connection — it silently
 * leaves the stream up, so the assertion times out against a perfectly healthy dot.
 *
 * Owning the route from before the first navigation is stronger anyway: it is deterministic,
 * it takes milliseconds instead of waiting out a 20s network timeout, and it can produce the
 * two failure modes separately — a stream that dropped after being live, and one that never
 * connected at all. The UI is supposed to say different things about those.
 */

/** A complete, valid SSE response that ENDS — which is exactly what a dropped stream is. */
const ONE_SHOT_STREAM = [
  "retry: 1000",
  "",
  `data: ${JSON.stringify({
    type: "hello",
    mode: "simulated",
    tick_ms: 500,
    poll_interval_s: 0.5,
    session_date: "2026-08-17",
    healthy: true,
    quotes: [],
  })}`,
  "",
  "",
].join("\n");

/**
 * First connection succeeds and then ends; every retry after it is refused.
 *
 * The first response is what sets the hook's `everOpen` flag, so the state that follows is
 * "reconnecting" rather than "connecting" — the distinction this is here to check.
 */
async function serveThenRefuse(page: Page): Promise<void> {
  let first = true;
  await page.route("**/api/stream/prices", async (route) => {
    if (first) {
      first = false;
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: ONE_SHOT_STREAM,
      });
      return;
    }
    await route.abort("connectionfailed");
  });
}

test.describe("SSE resilience", () => {
  test.beforeEach(async ({ request }) => {
    await resetAccount(request);
  });

  test("a dropped stream reads as RECONNECTING, not as a first connection", async ({
    page,
  }) => {
    await serveThenRefuse(page);
    await page.goto("/");

    // "reconnecting" is only reachable once the stream has been open, so this single
    // assertion covers both halves: the UI went live, and it then noticed the drop instead
    // of freezing on stale prices while still claiming LIVE FEED.
    await expect(page.getByTestId("connection-dot")).toHaveAttribute(
      "data-state",
      /reconnecting|closed/,
    );
    await expect(page.getByTestId("connection-label")).toHaveText(
      /RECONNECTING|DISCONNECTED/,
    );
  });

  test("a stream that never connects never claims to be live", async ({ page }) => {
    await page.route("**/api/stream/prices", (route) => route.abort("connectionfailed"));
    await page.goto("/");

    await expect(page.getByTestId("connection-label")).not.toHaveText("LIVE FEED");
    // The rest of the page still has to work — the portfolio comes from REST, not the stream.
    await expect(page.getByTestId("stat-cash")).toContainText("$10,000.00");
  });

  test("the stream recovers on its own, and prices resume ticking", async ({ page }) => {
    await serveThenRefuse(page);
    await page.goto("/");
    await expect(page.getByTestId("connection-dot")).toHaveAttribute(
      "data-state",
      /reconnecting|closed/,
    );

    // Stop interfering. The server sends `retry: 1000`, so the browser reconnects unaided —
    // nothing in the app has to be told to try again.
    await page.unroute("**/api/stream/prices");
    await waitForStream(page);

    const priceCell = page.getByTestId("watchlist-row-MU").locator("td").nth(1);
    await expect(priceCell).toContainText("$");
    const settled = await priceCell.textContent();
    await expect(priceCell).not.toHaveText(settled ?? "");
  });
});
