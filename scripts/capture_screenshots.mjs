/**
 * Capture deterministic product screenshots through Chrome DevTools Protocol.
 *
 * Requires the API/UI dev servers and a local Chrome/Edge installation. The
 * script uses an isolated temporary profile and a sessionStorage API key; it
 * never writes the key to a screenshot or to disk.
 */
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { join } from "node:path";
import process from "node:process";

const ROOT = process.cwd();
const OUT = join(ROOT, "docs", "assets", "phase14");
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
  const profile = join(ROOT, "artifacts", `phase14-chrome-${Date.now()}`);
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
    await sleep(1200);
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

    const setPreferences = async (language, theme) => {
      await evaluate(
        cdp,
        `localStorage.setItem("codepilot.language", ${JSON.stringify(language)});
         localStorage.setItem("codepilot.theme", ${JSON.stringify(theme)});
         true`,
      );
      await cdp.send("Page.reload");
      await sleep(1400);
    };

    const navigate = async (hash) => {
      await evaluate(cdp, `location.href = ${JSON.stringify(`${BASE}/${hash}`)}; true`);
      await sleep(1400);
      console.log(`at ${await evaluate(cdp, "location.href")}`);
    };

    await setPreferences("zh-CN", "dark");
    await navigate("#/new");
    await shot("01-new-task-zh-dark.png");

    await navigate("#/settings");
    await sleep(500);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("nav button")].find((b) => b.textContent.includes("模型与供应商"))?.click(); true`,
    );
    await sleep(700);
    await shot("02-settings-models-zh-dark.png");

    if (TASK_ID) {
      await navigate(`#/task/${TASK_ID}`);
      await sleep(1200);
      await shot("03-session-timeline-zh-dark.png");
    }

    await setPreferences("en-US", "light");
    await navigate("#/new");
    await shot("04-new-task-en-light.png");

    await navigate("#/settings");
    await sleep(500);
    await evaluate(
      cdp,
      `[...document.querySelectorAll("nav button")].find((b) => b.textContent.includes("General"))?.click(); true`,
    );
    await sleep(500);
    await shot("05-settings-general-en-light.png");

    await navigate("#/history");
    await sleep(700);
    await shot("06-history-en-light.png");

    cdp.close();
  } finally {
    chrome.kill();
  }
}

void main().catch((error) => {
  console.error(error);
  process.exit(1);
});
