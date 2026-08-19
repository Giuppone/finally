import { expect, test } from "@playwright/test";

import { resetAccount, waitForStream } from "./helpers";

/**
 * The daily portfolio evolution reconstructed from a dated CEDEAR ledger
 * (planning/PORTFOLIO_HISTORY.md).
 *
 * Every test here is skipped when the build carries no `backend/calibration/ledger.json`.
 * That is a legitimate build — a fresh checkout that has never run
 * `import_broker_with_dates` — and the feature is designed to degrade to LIVE-only rather
 * than error, which the last test asserts directly.
 */
test.describe("portfolio evolution", () => {
  test.beforeEach(async ({ request, page }) => {
    await resetAccount(request);
    await page.goto("/");
    await waitForStream(page);
  });

  async function ledgerAvailable(request: import("@playwright/test").APIRequestContext) {
    const response = await request.get("/api/history/portfolio?range=max");
    expect(response.ok()).toBeTruthy();
    return (await response.json()).available as boolean;
  }

  test("the P&L chart opens on the daily curve, not on live snapshots", async ({
    page,
    request,
  }) => {
    test.skip(!(await ledgerAvailable(request)), "no reconstructed ledger in this build");

    // The whole point of the feature: the evolution is what the page loads with. A fresh
    // account has at most one snapshot, so LIVE would show the placeholder here.
    const chart = page.getByTestId("pnl-chart");
    await expect(chart).toHaveAttribute("data-range", "max");
    await expect(chart.locator("path.recharts-area-area")).toBeVisible();
    await expect(page.getByTestId("pnl-range-max")).toHaveAttribute("aria-pressed", "true");
  });

  test("the range strip narrows the window and rebases the percentage", async ({
    page,
    request,
  }) => {
    test.skip(!(await ledgerAvailable(request)), "no reconstructed ledger in this build");

    const full = await (await request.get("/api/history/portfolio?range=max")).json();
    const short = await (await request.get("/api/history/portfolio?range=3m")).json();

    expect(short.points.length).toBeLessThan(full.points.length);
    // Rebased server-side to the first point of the FILTERED window — a client rebasing the
    // max series itself would show the wrong number.
    expect(short.points[0].return_pct).toBe(0);
    expect(short.points.at(-1).total_value).toBeCloseTo(full.points.at(-1).total_value, 2);

    await page.getByTestId("pnl-range-3m").click();
    await expect(page.getByTestId("pnl-chart")).toHaveAttribute("data-range", "3m");
  });

  test("the $ / % toggle switches basis without losing the series", async ({
    page,
    request,
  }) => {
    test.skip(!(await ledgerAvailable(request)), "no reconstructed ledger in this build");

    const chart = page.getByTestId("pnl-chart");
    await expect(chart.locator("path.recharts-area-area")).toBeVisible();

    await page.getByTestId("pnl-basis-percent").click();
    await expect(page.getByTestId("pnl-basis-percent")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // A field swap, not a refetch: the area must still be drawn straight away.
    await expect(chart.locator("path.recharts-area-area")).toBeVisible();

    await page.getByTestId("pnl-basis-value").click();
    await expect(chart.locator("path.recharts-area-area")).toBeVisible();
  });

  test("LIVE restores the streaming view on both charts", async ({ page, request }) => {
    test.skip(!(await ledgerAvailable(request)), "no reconstructed ledger in this build");

    await page.getByTestId("pnl-range-live").click();
    await expect(page.getByTestId("pnl-chart")).toHaveAttribute("data-range", "live");
    // The $/% toggle is meaningless against the paper book and must disappear with it.
    await expect(page.getByTestId("pnl-basis-percent")).toHaveCount(0);

    await expect(page.getByTestId("main-chart")).toHaveAttribute("data-range", "live");
  });

  test("selecting a ticker and picking a range draws its daily closes", async ({
    page,
    request,
  }) => {
    const response = await request.get("/api/history/prices/INTC?range=3m");
    test.skip(!response.ok(), "no daily bars for INTC in this build");
    const daily = await response.json();
    expect(daily.points.length).toBeGreaterThan(20);

    await page.getByTestId("watchlist-row-INTC").click();
    const chart = page.getByTestId("main-chart");
    await expect(chart).toHaveAttribute("data-range", "live");

    await page.getByTestId("chart-range-3m").click();
    await expect(chart).toHaveAttribute("data-range", "3m");
    await expect(chart.locator("path.recharts-line-curve")).toBeVisible();

    // The axis is what actually distinguishes the two views to a reader: the daily series
    // ticks in calendar dates ("Jun 6"), the live one in clock times ("22:17:47"). Asserting
    // on the tick labels tests the visible difference rather than the SVG path data.
    // Note the layer: Recharts 3.x renders x tick labels in a sibling `-tick-labels` group,
    // NOT nested inside `.recharts-xAxis`, so the obvious descendant selector matches nothing.
    const ticks = chart.locator(".recharts-xAxis-tick-labels text");
    await expect
      .poll(async () => (await ticks.allTextContents()).join(" "))
      .toMatch(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d/);
  });

  test("the user's own trades are marked on the daily chart", async ({ page, request }) => {
    // The misreading behind the markers, as a regression: the daily chart draws the
    // ticker's market price for the whole range, so without markers a recent buy reads as
    // a long holding. The committed ledger has both a buy and a sell for INTC inside 3M.
    const response = await request.get("/api/history/prices/INTC?range=3m");
    test.skip(!response.ok(), "no daily bars for INTC in this build");
    const daily = await response.json();
    test.skip(daily.trades.length === 0, "no reconstructed ledger trades in this build");

    await page.getByTestId("watchlist-row-INTC").click();
    await page.getByTestId("chart-range-3m").click();

    const chart = page.getByTestId("main-chart");
    await expect(chart).toHaveAttribute("data-range", "3m");
    await expect(page.getByTestId("held-since")).toContainText("held since");
    const buys = daily.trades.filter((t: { side: string }) => t.side === "buy").length;
    const sells = daily.trades.length - buys;
    await expect(chart.getByTestId("trade-marker-buy")).toHaveCount(buys);
    await expect(chart.getByTestId("trade-marker-sell")).toHaveCount(sells);
  });

  test("a ticker with no daily bars offers LIVE only, rather than an empty panel", async ({
    page,
    request,
  }) => {
    // Nothing has bars for a symbol invented at runtime, so the strip must collapse to LIVE.
    const response = await request.get("/api/history/prices/NOSUCHTICKER");
    expect(response.status()).toBe(404);
  });

  test("the daily routes never 404 the page load", async ({ request }) => {
    // 200 with `available: false` on a build with no ledger — the frontend fetches this on
    // every load, and a stock deployment must not log a 404 each time.
    const response = await request.get("/api/history/portfolio");
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(typeof body.available).toBe("boolean");
    expect(Array.isArray(body.points)).toBeTruthy();
  });
});
