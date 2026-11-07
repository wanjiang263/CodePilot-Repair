import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(web);
const python = process.env.CODEPILOT_E2E_PYTHON ??
  path.join(root, ".venv", existsSync(path.join(root, ".venv", "Scripts")) ? "Scripts/python.exe" : "bin/python");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  outputDir: "../output/playwright/results",
  reporter: [["list"], ["json", { outputFile: "../output/playwright/results.json" }]],
  use: {
    baseURL: "http://127.0.0.1:8765/app/",
    locale: "en-US",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run build && "${python}" e2e/server.py`,
    cwd: web,
    url: "http://127.0.0.1:8765/health",
    reuseExistingServer: false,
    timeout: 120_000,
    env: { CODEPILOT_API_ALLOW_LIVE_LLM: "false", PYTHONUTF8: "1" },
  },
});
