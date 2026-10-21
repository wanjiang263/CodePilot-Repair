import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enUS from "../locales/en-US/index.json";
import zhCN from "../locales/zh-CN/index.json";

export type LanguagePreference = "system" | "zh-CN" | "en-US";
export type Language = "zh-CN" | "en-US";

const STORAGE_KEY = "codepilot.language";

export const LANGUAGES: Language[] = ["zh-CN", "en-US"];

export function isLanguage(value: unknown): value is Language {
  return value === "zh-CN" || value === "en-US";
}

export function resolveLanguage(preference: LanguagePreference): Language {
  if (preference !== "system") return preference;
  if (typeof navigator === "undefined") return "en-US";
  const browser = navigator.languages?.[0] ?? navigator.language ?? "";
  return browser.toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
}

export function storedLanguagePreference(): LanguagePreference {
  if (typeof window === "undefined") return "system";
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "system" || isLanguage(value) ? value : "system";
  } catch {
    return "system";
  }
}

export function setLanguagePreference(preference: LanguagePreference): void {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(STORAGE_KEY, preference);
    } catch {
      // Hardened browsers can disable storage; the in-memory language still works.
    }
  }
  const language = resolveLanguage(preference);
  void i18n.changeLanguage(language);
  if (typeof document !== "undefined") {
    document.documentElement.lang = language;
  }
}

void i18n.use(initReactI18next).init({
  resources: {
    "zh-CN": { translation: zhCN },
    "en-US": { translation: enUS },
  },
  lng: resolveLanguage(storedLanguagePreference()),
  fallbackLng: "en-US",
  supportedLngs: LANGUAGES,
  initAsync: false,
  interpolation: { escapeValue: false },
  returnNull: false,
});

if (typeof document !== "undefined") {
  document.documentElement.lang = i18n.language;
}

if (typeof window !== "undefined") {
  window.addEventListener("languagechange", () => {
    if (storedLanguagePreference() === "system") {
      void i18n.changeLanguage(resolveLanguage("system"));
    }
  });
}

export default i18n;
