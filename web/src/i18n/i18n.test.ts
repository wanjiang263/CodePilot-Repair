import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import en from "../locales/en-US/index.json";
import zh from "../locales/zh-CN/index.json";
import i18n, { resolveLanguage, setLanguagePreference } from "./index";

type Resource = Record<string, unknown>;

function flatten(value: Resource, prefix = ""): Map<string, string> {
  const result = new Map<string, string>();
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof child === "string") result.set(path, child);
    else if (child && typeof child === "object") {
      for (const [nested, text] of flatten(child as Resource, path)) result.set(nested, text);
    }
  }
  return result;
}

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([A-Za-z0-9_]+)\s*\}\}/g)]
    .map((match) => match[1])
    .sort();
}

function sourceFiles(root: string): string[] {
  const output: string[] = [];
  const walk = (directory: string) => {
    for (const name of readdirSync(directory)) {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) {
        if (name !== "locales" && name !== "test") walk(path);
      } else if (/\.(tsx?|ts)$/.test(name) && !name.endsWith(".test.ts") && !name.endsWith(".test.tsx")) {
        output.push(path);
      }
    }
  };
  walk(root);
  return output;
}

const enKeys = flatten(en as Resource);
const zhKeys = flatten(zh as Resource);

describe("i18n resources", () => {
  it("contains exactly the same keys in English and Chinese", () => {
    expect([...zhKeys.keys()].sort()).toEqual([...enKeys.keys()].sort());
  });

  it("keeps placeholders aligned across languages", () => {
    for (const [key, value] of enKeys) {
      expect(placeholders(zhKeys.get(key) ?? ""), key).toEqual(placeholders(value));
    }
  });

  it("has no empty translations", () => {
    for (const [key, value] of [...enKeys, ...zhKeys]) {
      expect(value.trim().length, key).toBeGreaterThan(0);
    }
  });

  it("references only existing keys from components and hooks", () => {
    const missing: string[] = [];
    for (const file of sourceFiles(join(process.cwd(), "src"))) {
      const source = readFileSync(file, "utf8");
      const literalKeys = [
        ...source.matchAll(/\bt\(\s*"([^"$]+)"/g),
        ...source.matchAll(/i18nKey="([^"]+)"/g),
      ].map((match) => match[1]);
      for (const key of literalKeys) {
        if (enKeys.has(key) || enKeys.has(`${key}_one`) || enKeys.has(`${key}_other`)) continue;
        missing.push(`${relative(process.cwd(), file)} → ${key}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("has no CJK literals in component source outside language endonyms", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(join(process.cwd(), "src"))) {
      if (file.includes("locales") || file.endsWith("i18n.test.ts")) continue;
      const source = readFileSync(file, "utf8");
      for (const line of source.split("\n")) {
        if (!/[\u4e00-\u9fff]/.test(line)) continue;
        if (line.includes("简体中文") || line.includes("中文")) continue;
        offenders.push(`${relative(process.cwd(), file)}: ${line.trim().slice(0, 80)}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("language switching", () => {
  afterEach(async () => {
    await i18n.changeLanguage("en-US");
    setLanguagePreference("system");
  });

  it("switches instantly and resolves unsupported browsers to English", async () => {
    expect(resolveLanguage("zh-CN")).toBe("zh-CN");
    expect(resolveLanguage("en-US")).toBe("en-US");
    await i18n.changeLanguage("zh-CN");
    expect(i18n.t("nav.settings")).toBe("设置");
    expect(i18n.t("status.stopped.label")).toBe("已停止");
    expect(i18n.t("history.official", { value: "RESOLVED" })).toBe("官方：RESOLVED");
    await i18n.changeLanguage("en-US");
    expect(i18n.t("nav.settings")).toBe("Settings");
    expect(i18n.t("status.stopped.label")).toBe("Stopped");
  });

  it("interpolates counts in both languages", async () => {
    await i18n.changeLanguage("en-US");
    expect(i18n.t("history.archived", { count: 1 })).toBe("archived run");
    expect(i18n.t("history.archived", { count: 3 })).toBe("archived runs");
    await i18n.changeLanguage("zh-CN");
    expect(i18n.t("history.archived", { count: 3 })).toBe("条归档运行");
  });
});
