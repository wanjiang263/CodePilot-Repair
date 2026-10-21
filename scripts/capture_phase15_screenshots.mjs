/**
 * Phase 15 browser acceptance: captures real Chrome screenshots and asserts the
 * UI states that changed in this phase. Requires the API and Vite dev servers.
 */
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { join } from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const OUT = join(ROOT, "docs", "assets", "phase15");
const BASE = "http://127.0.0.1:5173";
const API_KEY = process.env.CODEPILOT_CAPTURE_API_KEY;
const TASK_ID = process.env.CODEPILOT_CAPTURE_TASK_ID ?? "";
const CHROME =
  process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";

if (!API_KEY) {
  console.error("CODEPILOT_CAPTURE_API_KEY is required");
  process.exit(2);
}

async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class CDP {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
  }

  async connect() {
    this.ws = new WebSocket(this.url);
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id) return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.ws?.close();
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text ?? "Runtime.evaluate failed");
  }
  return result.result?.value;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const port = await freePort();
  const profile = join(ROOT, "artifacts", `phase15-chrome-${Date.now()}`);
  const chrome = spawn(
    CHROME,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-first-run",
      "--no-default-browser-check",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      "--window-size=1440,900",
      "about:blank",
    ],
    { stdio: "ignore", windowsHide: true },
  );

  try {
    let target = null;
    for (let attempt = 0; attempt < 60 && !target; attempt += 1) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(BASE)}`, {
          method: "PUT",
        });
        if (response.ok) target = await response.json();
      } catch {
        await sleep(200);
      }
    }
    if (!target) throw new Error("Chrome DevTools endpoint did not become ready");

    const cdp = new CDP(target.webSocketDebuggerUrl);
    await cdp.connect();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp.send("Page.navigate", { url: BASE });
    await sleep(1800);
    console.log(`loaded ${await evaluate(cdp, "location.href")}`);
    await evaluate(
      cdp,
      `sessionStorage.setItem("codepilot.apiKey", ${JSON.stringify(API_KEY)}); true`,
    );
    await cdp.send("Page.reload");
    await sleep(1500);

    const shot = async (name) => {
      const result = await cdp.send("Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: false,
      });
      await writeFile(join(OUT, name), Buffer.from(result.data, "base64"));
      console.log(`saved ${name}`);
    };

    const assert = async (label, expression) => {
      const value = await evaluate(cdp, expression);
      if (!value) throw new Error(`assertion failed: ${label}`);
      console.log(`ok ${label}`);
    };

    const setPreferences = async (language, theme) => {
      await evaluate(
        cdp,
        `localStorage.setItem("codepilot.language", ${JSON.stringify(language)});
         localStorage.setItem("codepilot.theme", ${JSON.stringify(theme)});
         true`,
      );
      await cdp.send("Page.reload");
      await sleep(1600);
    };

    const navigate = async (hash) => {
      await evaluate(cdp, `location.href = ${JSON.stringify(`${BASE}/${hash}`)}; true`);
      await sleep(1600);
      console.log(`at ${await evaluate(cdp, "location.href")}`);
    };

    await setPreferences("zh-CN", "dark");
    await navigate("#/new");
    await assert(
      "zh dark new task after reload",
      `document.body.textContent.includes("新建修复会话") &&
       document.documentElement.classList.contains("dark") &&
       localStorage.getItem("codepilot.language") === "zh-CN"`,
    );
    await shot("01-new-task-zh-dark.png");

    await navigate("#/settings");
    await sleep(600);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("nav button")].find((b) => b.textContent.includes("模型与供应商"))?.click(); true`,
    );
    await sleep(900);
    await assert(
      "provider page shows long provider and truthful states",
      `document.body.textContent.includes("Research Gateway With A Deliberately Long Provider Name") &&
       document.body.textContent.includes("Live 路由条件满足") &&
       document.body.textContent.includes("真实推理未测试") &&
       document.body.textContent.includes("模板价 · 待核实")`,
    );
    await assert(
      "provider page does not overflow the viewport",
      `document.documentElement.scrollWidth <= window.innerWidth + 1`,
    );
    await evaluate(
      cdp,
      `[...document.querySelectorAll("button[aria-label='更多操作']")][0]?.click(); true`,
    );
    await sleep(300);
    await assert(
      "provider actions remain reachable",
      `document.querySelectorAll("[role='menuitem']").length >= 4`,
    );
    await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); true`);
    await sleep(200);
    await shot("02-settings-models-zh-dark.png");
    await evaluate(
      cdp,
      `(() => {
         const input = document.querySelector("input[aria-label='按供应商筛选']");
         const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
         setter.call(input, "no-such-provider");
         input.dispatchEvent(new Event("input", {bubbles: true}));
         return true;
       })()`,
    );
    await sleep(400);
    await assert(
      "provider search has an explicit empty state",
      `document.body.textContent.includes("没有匹配的供应商或模型")`,
    );
    await evaluate(
      cdp,
      `(() => {
         const input = document.querySelector("input[aria-label='按供应商筛选']");
         const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
         setter.call(input, "");
         input.dispatchEvent(new Event("input", {bubbles: true}));
         return true;
       })()`,
    );
    await sleep(400);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("span")].find(
         (el) => el.textContent.trim() === "Research Gateway With A Deliberately Long Provider Name",
       )?.scrollIntoView({block: "center"}); true`,
    );
    await sleep(500);
    await assert(
      "long provider and model ids remain visible",
      `document.body.textContent.includes("internal-org/acme-very-long-model-identifier-2026-10-21-preview")`,
    );
    await shot("10-settings-long-provider-zh-dark.png");
    await evaluate(
      cdp,
      `(() => {
         const marker = [...document.querySelectorAll("span")].find(
           (el) => el.textContent.trim() === "openai-unconfigured",
         );
         const card = marker?.closest("div.rounded-xl");
         const button = [...(card?.querySelectorAll("button") ?? [])].find(
           (el) => el.textContent.trim() === "校验配置",
         );
         button?.click();
         return Boolean(button);
       })()`,
    );
    await sleep(900);
    await assert(
      "configuration errors are surfaced in the UI",
      `document.querySelector("[role='dialog']")?.textContent.includes("配置需要处理")`,
    );
    await evaluate(cdp, `document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"})); true`);
    await sleep(300);

    if (TASK_ID) {
      await navigate(`#/task/${TASK_ID}`);
      await sleep(1400);
      await assert(
        "timeline renders one independent review stage",
        `(() => {
          const root = document.querySelector("[data-testid='timeline-stages']");
          if (!root) return false;
          const labels = [...root.querySelectorAll("*")].filter(
            (el) => el.children.length === 0 && el.textContent.trim() === "独立审查",
          );
          return labels.length === 1;
        })()`,
      );
      await shot("03-session-timeline-zh-dark.png");
    }

    await setPreferences("en-US", "light");
    await navigate("#/new");
    await assert(
      "en light new task after reload",
      `document.body.textContent.includes("New repair session") &&
       !document.documentElement.classList.contains("dark") &&
       localStorage.getItem("codepilot.theme") === "light"`,
    );
    await shot("04-new-task-en-light.png");

    await navigate("#/settings");
    await sleep(600);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("nav button")].find((b) => b.textContent.includes("General"))?.click(); true`,
    );
    await sleep(600);
    await shot("05-settings-general-en-light.png");

    await navigate("#/history");
    await sleep(700);
    await assert(
      "history is readable in en light",
      `document.body.textContent.includes("History") || document.body.textContent.includes("Replay")`,
    );
    await shot("06-history-en-light.png");

    // System theme must follow the emulated OS preference both on load and live.
    await evaluate(
      cdp,
      `localStorage.setItem("codepilot.theme", "system"); true`,
    );
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "dark" }],
    });
    await cdp.send("Page.reload");
    await sleep(1500);
    await assert("system follows dark OS", `document.documentElement.classList.contains("dark")`);
    await shot("07-settings-system-dark-en.png");
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    await sleep(900);
    await assert(
      "system follows a live OS theme change",
      `!document.documentElement.classList.contains("dark")`,
    );
    await shot("08-settings-system-light-en.png");

    // Narrow desktop: the page itself must not overflow; tables may scroll inside.
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1100,
      height: 800,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await setPreferences("zh-CN", "dark");
    await navigate("#/settings");
    await sleep(600);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("nav button")].find((b) => b.textContent.includes("模型与供应商"))?.click(); true`,
    );
    await sleep(800);
    await assert(
      "narrow desktop does not overflow",
      `document.documentElement.scrollWidth <= window.innerWidth + 1`,
    );
    await shot("09-settings-models-zh-dark-1100.png");

    cdp.close();
  } finally {
    chrome.kill();
  }
}

void main().catch((error) => {
  console.error(error);
  process.exit(1);
});
