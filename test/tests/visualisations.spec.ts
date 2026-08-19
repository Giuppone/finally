import { expect, test } from "@playwright/test";

import { resetAccount, tradeViaBar, waitForStream } from "./helpers";

test.describe("portfolio visualisations", () => {
  test.beforeEach(async ({ request, page }) => {
    await resetAccount(request);
    await page.goto("/");
    await waitForStream(page);
  });

  test("the heatmap renders a tile per position, coloured by P&L", async ({ page }) => {
    await expect(page.getByTestId("heatmap")).toContainText("No positions yet");

    await tradeViaBar(page, "MU", 2, "buy");
    await tradeViaBar(page, "AMD", 3, "buy");

    const heatmap = page.getByTestId("heatmap");
    await expect(heatmap.locator("svg")).toBeVisible();
    await expect.poll(async () => heatmap.locator("rect").count()).toBeGreaterThanOrEqual(2);

    // Tiles are filled with a green or red tint keyed to P&L — never a flat default.
    const fills = await heatmap.locator("rect").evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("fill") ?? ""),
    );
    expect(fills.some((fill) => /rgba\((63|248|139)/.test(fill))).toBeTruthy();
  });

  test("the P&L chart accumulates points after trades", async ({ page }) => {
    const chart = page.getByTestId("pnl-chart");

    // Pin to LIVE first. The panel now opens on MAX, which draws the reconstructed daily
    // curve from a committed artifact — that renders an area path whether or not a single
    // snapshot was ever written, so asserting on it here would prove nothing about
    // portfolio_snapshots, which is what this spec is for.
    await page.getByTestId("pnl-range-live").click();
    await expect(chart).toHaveAttribute("data-range", "live");

    await tradeViaBar(page, "SLV", 5, "buy");

    // TWO trades, a beat apart, on purpose. A snapshot is written immediately after every
    // trade, but `_snapshot` skips one whose total value has not moved by more than half a
    // cent — and a trade barely moves total value, since the cash leaving equals the
    // position arriving. So the first buy writes the one point a fresh account has, and the
    // second only writes because prices ticked in between. One point is not a line.
    //
    // Waiting on the 30s background snapshot task for that second point instead is what used
    // to make this spec flaky: whether it passed depended on where the trade happened to
    // land inside that 30s cycle.
    await page.waitForTimeout(1_500);
    await tradeViaBar(page, "SLV", 1, "buy");

    await expect.poll(async () => chart.locator("svg").count()).toBeGreaterThan(0);
    await expect(chart.locator("path.recharts-area-area")).toBeVisible();
  });

  test("the positions table reports cost, price and P&L per holding", async ({ page }) => {
    await tradeViaBar(page, "LRCX", 2, "buy");

    const row = page.getByTestId("position-row-LRCX");
    await expect(row).toBeVisible();

    // Seven populated columns: ticker, qty, avg cost, price, value, P&L, %.
    const cells = await row.locator("td").allTextContents();
    expect(cells).toHaveLength(7);
    expect(cells[2]).toMatch(/^\$/);   // avg cost
    expect(cells[3]).toMatch(/^\$/);   // live price
    expect(cells[6]).toMatch(/%$/);    // percent change
  });
});
