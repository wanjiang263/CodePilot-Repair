import { expect, test, type Page } from "@playwright/test";

const key = "browser-fixture-key-not-a-provider-secret";

async function connect(page: Page) {
  await page.goto("");
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Issue description" })).toBeVisible();
}

test("authentication rejects an invalid key and keeps the accepted key session-only", async ({ page }) => {
  await page.goto("");
  await page.getByLabel("API key", { exact: true }).fill("invalid-fixture-key");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText(/key.*rejected|invalid.*key|authentication/i)).toBeVisible();
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Issue description" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("codepilot.apiKey"))).toBe(key);
  expect(await page.evaluate(() => localStorage.getItem("codepilot.apiKey"))).toBeNull();
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Issue description" })).toBeVisible();
});

test("settings save through the real API and survive a reload", async ({ page }) => {
  await connect(page);
  await page.goto("#/settings");
  await page.getByRole("button", { name: "Dark", exact: true }).click();
  await expect(page.getByText(/Settings saved/)).toBeVisible();
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  const response = await page.request.get("/api/v1/settings", { headers: { "X-API-Key": key } });
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).general.theme).toBe("dark");
});

test("creates an offline task, observes completion, and displays the real patch/report", async ({ page }) => {
  await connect(page);
  await page.getByRole("button", { name: "Use example issue", exact: true }).click();
  const created = page.waitForResponse(r => r.url().endsWith("/api/v1/tasks") && r.request().method() === "POST");
  await page.getByRole("button", { name: /Start repair/ }).click();
  const task = await (await created).json();
  await expect(page).toHaveURL(/#\/task\//);
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/tasks/${task.task_id}`, { headers: { "X-API-Key": key } });
    return (await response.json()).ui_status;
  }, { timeout: 60_000 }).toBe("completed");
  await expect(page.getByText(/succeeded|completed/i).first()).toBeVisible();
  await page.getByRole("tab", { name: /Changes/ }).click();
  await expect(page.locator(".diff-add").first()).toContainText("return left + right");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await expect(page.getByRole("tabpanel").locator("pre")).toContainText(/verification|simulated_llm/i);
  await page.screenshot({ path: "../output/playwright/completed-task.png", fullPage: true });
});

test("network errors are visible and retry recovers", async ({ page }) => {
  await connect(page);
  const created = await page.request.post("/api/v1/tasks", {
    headers: { "X-API-Key": key },
    data: { repository: { kind: "demo", value: "mini_calc" },
      issue: "Browser retry recovery fixture: add(2, 3) must return 5.", execution_mode: "offline_fake" },
  });
  expect(created.ok()).toBeTruthy();
  const task = await created.json();
  await page.route("**/api/v1/tasks/**", route => route.abort("failed"));
  await page.goto(`#/task/${task.task_id}`);
  await expect(page.getByText("Could not load this session.", { exact: true })).toBeVisible();
  await page.unroute("**/api/v1/tasks/**");
  await page.getByRole("button", { name: /Retry/, exact: true }).click();
  await expect(page.getByText("Could not load this session.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", {
    name: "Browser retry recovery fixture: add(2, 3) must return 5.", exact: true,
  })).toBeVisible();
});

test("missing task displays the API error", async ({ page }) => {
  await connect(page);
  await page.goto("#/task/missing-fixture");
  await expect(page.getByText(/Task not found/)).toBeVisible();
});
