import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",

  // Serial, single worker, on purpose. FinAlly is deliberately single-user (PLAN.md §7):
  // one cash balance, one watchlist, one conversation. Parallel specs would trade against
  // each other's balance and reset each other's fixtures mid-assertion.
  fullyParallel: false,
  workers: 1,

  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 15_000 },

  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],

  use: {
    // Compose sets this to http://app:8000; a local run defaults to the published port.
    baseURL: process.env.BASE_URL ?? "http://localhost:8000",
    // NOTE: baseURL must stay a loopback address under compose. Chromium auto-upgrades
    // http:// to https:// for any non-localhost host, which is why the Playwright container
    // shares the app's network namespace — see the comment in docker-compose.test.yml.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    viewport: { width: 1600, height: 900 },
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
