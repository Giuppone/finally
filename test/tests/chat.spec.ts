import { expect, test } from "@playwright/test";

import { moneyAt, resetAccount, waitForStream } from "./helpers";

/**
 * These run against LLM_MOCK=true (set in docker-compose.test.yml), so replies are
 * deterministic, free and offline — no OpenRouter call happens (PLAN.md §9, §12).
 */
test.describe("AI chat", () => {
  test.beforeEach(async ({ request, page }) => {
    await resetAccount(request);
    await page.goto("/");
    await waitForStream(page);
  });

  test("sends a message and shows the reply", async ({ page }) => {
    await expect(page.getByText("MOCK")).toBeVisible();

    await page.getByTestId("chat-input").fill("how is my portfolio doing?");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("chat-message-user")).toContainText(
      "how is my portfolio doing?",
    );
    await expect(page.getByTestId("chat-message-assistant")).toContainText("[mock]");
  });

  test("executes a trade from chat and confirms it inline", async ({ page }) => {
    const cashBefore = await moneyAt(page, "stat-cash");

    await page.getByTestId("chat-input").fill("buy 3 MU");
    await page.getByTestId("chat-send").click();

    // The action chip is the inline confirmation PLAN.md §10 asks for.
    const chip = page.getByTestId("chat-action-executed");
    await expect(chip).toBeVisible();
    await expect(chip).toContainText("Buy 3 MU");

    await expect(page.getByTestId("position-row-MU")).toBeVisible();
    await expect
      .poll(async () => await moneyAt(page, "stat-cash"), { timeout: 15_000 })
      .toBeLessThan(cashBefore);
  });

  test("reports a rejected trade instead of silently dropping it", async ({ page }) => {
    await page.getByTestId("chat-input").fill("buy 999999 MU");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("chat-action-rejected")).toContainText(
      /insufficient cash/i,
    );
    expect(await moneyAt(page, "stat-cash")).toBeCloseTo(10_000, 2);
  });

  test("manages the watchlist from chat", async ({ page }) => {
    await expect(page.getByTestId("watchlist-row-NVDA")).toHaveCount(0);

    await page.getByTestId("chat-input").fill("watch NVDA");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("chat-action-executed")).toBeVisible();
    await expect(page.getByTestId("watchlist-row-NVDA")).toBeVisible();
  });

  test("restores the conversation after a reload", async ({ page }) => {
    await page.getByTestId("chat-input").fill("buy 1 AMD");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("chat-action-executed")).toBeVisible();

    await page.reload();
    await waitForStream(page);

    // PLAN.md §13 item 4: a refresh must not show an empty panel the assistant still has
    // context for — and the action chips must come back with it.
    await expect(page.getByTestId("chat-message-user")).toContainText("buy 1 AMD");
    await expect(page.getByTestId("chat-action-executed")).toContainText("Buy 1 AMD");
  });
});
