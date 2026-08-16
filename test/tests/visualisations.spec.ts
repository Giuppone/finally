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

    await tradeViaBar(page, "SLV", 5, "buy");

    // A snapshot is written immediately after every trade, so the chart does not have to
    // wait out the 30s background interval to have something to draw.
    await expect
      .poll(async () => chart.locator("svg").count(), { timeout: 20_000 })
      .toBeGreaterThan(0);
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
