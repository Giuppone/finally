import { expect, test, type Page } from "@playwright/test";

import {
  equalWeights,
  numericAt,
  openAnalytics,
  resetAccount,
  seedPortfolio,
  waitForStream,
} from "./helpers";

/**
 * The Risk & Return and Suggest Rebalance buttons (PORTFOLIO_ANALYTICS.md §9).
 *
 * Books are seeded through `POST /api/session` BEFORE the page loads, so every assertion
 * runs against the exact weights the test asked for and no spec pays for a reload. Prices
 * still tick underneath, hence the tolerances — a weight measured a second after seeding has
 * already moved a little.
 */

const BASKET = ["MU", "AMD", "SLV", "PLTR", "MRVL"];

// One lopsided book, reused: 64% in a single high-volatility name is what gives the
// optimiser something large and obviously correct to propose.
const LOPSIDED = { MU: 0.64, MRVL: 0.16, PLTR: 0.1, AMD: 0.07, SLV: 0.03 };

/** Legs currently proposed. Zero when the panel is showing its "Already there" state. */
async function legCount(page: Page): Promise<number> {
  const table = page.getByTestId("rebalance-trades");
  return (await table.count()) === 0 ? 0 : table.locator("tbody tr").count();
}

test.describe("portfolio analytics", () => {
  test.beforeEach(async ({ request }) => {
    await resetAccount(request);
  });

  test("both buttons open one drawer on their own tab, and Esc closes it", async ({
    page,
    request,
  }) => {
    await seedPortfolio(request, equalWeights(BASKET));
    await page.goto("/");
    await waitForStream(page);

    await openAnalytics(page, "risk");
    await expect(page.getByTestId("risk-scatter")).toBeVisible();

    // The same surface, switched — not a second drawer.
    await page.getByTestId("analytics-tab-rebalance").click();
    await expect(page.getByTestId("rebalance-preview")).toBeVisible();
    await expect(page.getByTestId("risk-report")).toBeHidden();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("analytics-panel")).toBeHidden();

    await openAnalytics(page, "rebalance");
    await expect(page.getByTestId("rebalance-preview")).toBeVisible();
  });

  test("an equal-WEIGHT book is not an equal-RISK book", async ({ page, request }) => {
    // The thesis of the whole feature, and the reason the equal-weight seeder exists: the
    // money is flat by construction and the risk is not, because these names carry very
    // different volatilities.
    await seedPortfolio(request, equalWeights(BASKET));
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "risk");

    const rows = page.getByTestId("risk-report").locator("tbody tr");
    await expect(rows).toHaveCount(BASKET.length);

    // Columns are located by HEADER, not by index. Reading cells[3] positionally is how
    // this spec broke when a CAGR column was inserted before Risk share - it silently
    // started asserting on the wrong numbers rather than failing to find them.
    const columns = await page
      .getByTestId("risk-report")
      .locator("thead th")
      .allTextContents();
    const weightAt = columns.findIndex((c) => c.trim() === "Weight");
    const riskAt = columns.findIndex((c) => c.trim() === "Risk share");
    expect(weightAt, "Weight column").toBeGreaterThan(-1);
    expect(riskAt, "Risk share column").toBeGreaterThan(-1);

    const parsed = await rows.evaluateAll(
      (nodes, [w, r]) =>
        nodes.map((node) => {
          const cells = Array.from(node.querySelectorAll("td")).map((cell) =>
            Number.parseFloat((cell.textContent ?? "").replace(/%/g, "")),
          );
          return { weight: cells[w], riskShare: cells[r] };
        }),
      [weightAt, riskAt],
    );

    const flat = 100 / BASKET.length;
    for (const row of parsed) {
      expect(row.weight).toBeGreaterThan(flat - 1.5);
      expect(row.weight).toBeLessThan(flat + 1.5);
    }

    const shares = parsed.map((row) => row.riskShare);
    expect(Math.max(...shares) - Math.min(...shares)).toBeGreaterThan(4);
    // Risk share is a real decomposition, not a heuristic: the parts sum to the whole.
    expect(shares.reduce((sum, share) => sum + share, 0)).toBeCloseTo(100, 0);
  });

  test("the risk map plots the efficient frontier and says where you sit on it", async ({
    page,
    request,
  }) => {
    await seedPortfolio(request, equalWeights(BASKET));
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "risk");

    // Both axes are labelled — a scatter of unlabelled percentages is a puzzle, not a chart.
    const scatter = page.getByTestId("risk-scatter");
    await expect(scatter).toContainText("annualised volatility");
    await expect(scatter).toContainText("Expected return");
    await expect(scatter).toContainText("Efficient frontier");

    // The curve itself: Recharts draws the connecting line as a path in the scatter's line
    // layer, so its absence means the frontier came back empty.
    await expect(
      scatter.locator("path.recharts-curve").first(),
    ).toBeVisible();

    // And the sentence that actually answers "how far from optimal am I?".
    await expect(page.getByTestId("frontier-gap")).toContainText(
      /sits (inside|on) the frontier/,
    );
  });

  test("an equal-weight book is measurably inside the frontier", async ({ request }) => {
    // Equal weight looks sensible and is not optimal — which is the reason to draw the curve
    // at all. Asserted through the API so the numbers are exact rather than rendered.
    await seedPortfolio(request, equalWeights(BASKET));
    const stats = await (
      await request.post("/api/analytics/risk", { data: {} })
    ).json();

    expect(stats.frontier.length).toBeGreaterThan(5);
    expect(stats.frontier_gap.avoidable_volatility).toBeGreaterThan(0);
    expect(stats.frontier_gap.forgone_return).toBeGreaterThan(0);

    // Monotone: more risk, more return, all the way along.
    for (let i = 1; i < stats.frontier.length; i += 1) {
      expect(stats.frontier[i].volatility).toBeGreaterThan(stats.frontier[i - 1].volatility);
      expect(stats.frontier[i].expected_return).toBeGreaterThan(
        stats.frontier[i - 1].expected_return,
      );
    }
  });

  test("expected return is never shown without its basis and window", async ({
    page,
    request,
  }) => {
    // The drift is the simulator's damped value, not a forecast. An unlabelled number here
    // would be the one genuinely misleading thing this panel could do - and a measurement
    // with no date on it is only slightly better.
    await seedPortfolio(request, equalWeights(BASKET));
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "risk");

    const report = page.getByTestId("risk-report");
    await expect(page.getByTestId("risk-expected-return")).toContainText("%");
    await expect(report).toContainText("damped to ~10% of realised");
    // The calibration window, so a reader can see how old the model is.
    await expect(report).toContainText(/Measured from daily bars over\s+\d{4}-\d{2}-\d{2}/);
    await expect(report).toContainText(/\d+ trading days/);
  });

  test("the detail table shows measured CAGR beside the damped drift", async ({
    page,
    request,
  }) => {
    // The whole reason CAGR is displayed: it makes the damping auditable. On this basket
    // the two differ by an order of magnitude, which is exactly what should be visible.
    await seedPortfolio(request, equalWeights(BASKET));
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "risk");

    const header = page.getByTestId("risk-report").locator("thead");
    await expect(header).toContainText("CAGR");

    const stats = await (
      await request.post("/api/analytics/risk", { data: {} })
    ).json();

    // Every seeded ticker is calibrated, so every row carries a measured growth rate.
    for (const position of stats.positions) {
      expect(position.calibrated).toBe(true);
      expect(position.cagr).not.toBeNull();
    }
    expect(stats.warnings).toEqual([]);

    // CAGR is the undamped measurement; expected_return is ~10% of the log-drift it came
    // from. They must not be the same number, or the damping silently stopped happening.
    const mu = stats.positions.find((p: { ticker: string }) => p.ticker === "MU");
    expect(mu.cagr).toBeGreaterThan(mu.expected_return * 2);
    expect(mu.expected_return).toBeLessThanOrEqual(0.2);

    expect(stats.calibration.trading_days).toBeGreaterThan(60);
    expect(stats.calibration.start < stats.calibration.end).toBe(true);
  });

  test("min variance never increases volatility", async ({ page, request }) => {
    // The single most valuable assertion in the feature: if this fails, the optimiser is
    // wrong, whatever else looks right.
    await seedPortfolio(request, LOPSIDED);
    await page.goto("/");
    await waitForStream(page);

    await openAnalytics(page, "risk");
    const before = await numericAt(page, "risk-volatility");
    expect(before).toBeGreaterThan(0);

    await page.getByTestId("analytics-tab-rebalance").click();
    await page.getByTestId("objective-min_variance").click();
    await expect(page.getByTestId("rebalance-preview")).toBeVisible();

    const after = await numericAt(page, "after-volatility");
    expect(after).toBeLessThanOrEqual(before);
    // On a 64%-single-name book the improvement is substantial, not a rounding win.
    expect(after).toBeLessThan(before - 2);
    // Diversifying necessarily spreads the book over more effective names.
    expect(await numericAt(page, "after-effective-n")).toBeGreaterThan(1);
  });

  test("risk parity equalises the risk shares it proposes", async ({ page, request }) => {
    await seedPortfolio(request, LOPSIDED);
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "rebalance");

    await page.getByTestId("objective-risk_parity").click();
    await expect(page.getByTestId("rebalance-weights")).toBeVisible();

    // Read the proposal back through the risk endpoint: risk parity claims its targets
    // decompose into equal risk contributions, so check that they do.
    const plan = await (
      await request.post("/api/analytics/rebalance", {
        data: { objective: "risk_parity", constraints: { max_weight: 0.35 } },
      })
    ).json();

    const holdings = plan.targets
      .filter((row: { target_weight: number }) => row.target_weight > 0)
      .map((row: { ticker: string; target_weight: number }) => ({
        ticker: row.ticker,
        weight: row.target_weight,
      }));

    const risk = await (
      await request.post("/api/analytics/risk", { data: { holdings } })
    ).json();

    const shares = risk.positions.map((row: { risk_share: number }) => row.risk_share);
    expect(Math.max(...shares) - Math.min(...shares)).toBeLessThan(0.02);
  });

  test("suggesting does not trade", async ({ page, request }) => {
    // A button labelled "suggest" must not move the account, and nothing else in this suite
    // would catch it if it did.
    await seedPortfolio(request, LOPSIDED);
    await page.goto("/");
    await waitForStream(page);

    const before = await (await request.get("/api/session")).json();

    await openAnalytics(page, "rebalance");
    await expect(page.getByTestId("rebalance-trades")).toBeVisible();

    const after = await (await request.get("/api/session")).json();
    expect(after.cash_balance).toBeCloseTo(before.cash_balance, 6);
    expect(after.positions).toEqual(before.positions);
  });

  test("Apply executes the plan and leaves the book at its target", async ({
    page,
    request,
  }) => {
    await seedPortfolio(request, LOPSIDED);
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "rebalance");

    const planned = await numericAt(page, "after-volatility");
    const legs = await legCount(page);
    expect(legs).toBeGreaterThan(0);

    // Sells before buys — a buy-first order fails on insufficient cash for exactly the
    // trades that most need to happen.
    const sides = (
      await page
        .getByTestId("rebalance-trades")
        .locator("tbody tr td:nth-child(2)")
        .allTextContents()
    ).map((side) => side.trim().toLowerCase());
    const firstBuy = sides.indexOf("buy");
    const lastSell = sides.lastIndexOf("sell");
    if (firstBuy !== -1 && lastSell !== -1) expect(lastSell).toBeLessThan(firstBuy);

    await page.getByTestId("apply-rebalance").click();

    await expect(page.getByTestId("analytics-panel")).toContainText(/Filled all \d+ trades/);
    await expect(page.getByTestId("analytics-error")).toBeHidden();

    // The panel re-prices itself against the book that now exists, so what it shows is the
    // outcome rather than the prediction. Prices ticked during the batch, so this is a
    // couple of points of tolerance, not an equality.
    const realised = await numericAt(page, "after-volatility");
    expect(Math.abs(realised - planned)).toBeLessThan(3);

    const portfolio = await (await request.get("/api/portfolio")).json();
    expect(portfolio.cash_balance).toBeGreaterThanOrEqual(0);

    // No sliver left behind by a full exit: every surviving position is worth real money,
    // not $0.03 of a truncated sell (Review.md B11).
    for (const holding of portfolio.positions) {
      expect(holding.market_value).toBeGreaterThan(1);
    }

    // Rebalancing an already-rebalanced book has almost nothing left to do: every remaining
    // delta falls under the $10 dust floor. Catches a whole class of off-by-one in the
    // weight maths, which would otherwise show up as the plan never converging. Allowed to
    // be 1 rather than 0 because prices keep moving while the batch executes.
    expect(await legCount(page)).toBeLessThanOrEqual(1);
  });

  test("a name dropped from the selection is targeted at zero and sold down", async ({
    page,
    request,
  }) => {
    await seedPortfolio(request, LOPSIDED);
    await page.goto("/");
    await waitForStream(page);
    await openAnalytics(page, "rebalance");

    // Drop MU — the 64% position — from the selection. "Rebalance into these names" has to
    // mean something for the names left out.
    await page.getByTestId("analytics-pick-MU").click();
    await page.getByTestId("analytics-recalculate").click();

    const muRow = page
      .getByTestId("rebalance-trades")
      .locator("tbody tr")
      .filter({ hasText: "MU" });
    // "sell" in the DOM; the uppercase in the UI is CSS text-transform, which textContent
    // does not reflect.
    await expect(muRow.first()).toContainText("sell", { ignoreCase: true });
  });

  test("an impossible weight cap explains the arithmetic instead of failing quietly", async ({
    request,
  }) => {
    // Five names cannot all sit under a 15% cap, and "optimisation failed" would tell the
    // user nothing they could act on.
    await seedPortfolio(request, equalWeights(BASKET));
    const response = await request.post("/api/analytics/rebalance", {
      data: { objective: "min_variance", constraints: { max_weight: 0.15 } },
    });
    expect(response.status()).toBe(400);
    expect((await response.json()).detail).toContain("1/5");
  });

  test("an all-cash account still gets a usable panel", async ({ page, request }) => {
    // Nothing is held, so the drawer seeds its selection from the watchlist and models those
    // names at equal weight. Refusing to render until the user owns something would make the
    // feature undiscoverable exactly when it is most useful.
    await page.goto("/");
    await waitForStream(page);

    await openAnalytics(page, "risk");
    await expect(page.getByTestId("analytics-error")).toBeHidden();
    await expect(page.getByTestId("risk-volatility")).toContainText("%");
    await expect(page.getByTestId("risk-report").locator("tbody tr")).not.toHaveCount(0);
  });
});
