import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:15318",
    channel: process.platform === "win32" ? "chrome" : undefined,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${process.env.CONSOLE_TEST_PYTHON || "python"} tests/server.py`,
    url: "http://127.0.0.1:15318/healthz",
    reuseExistingServer: false,
    timeout: 20000,
  },
  reporter: "list",
  outputDir: "../.local/console-browser-results",
});
