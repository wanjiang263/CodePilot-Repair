import { useCallback, useEffect, useMemo, useState } from "react";

import {
  resolveLanguage,
  setLanguagePreference,
  storedLanguagePreference,
  type Language,
  type LanguagePreference,
} from "../i18n";

const THEME_KEY = "codepilot.theme";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function storedThemeMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  } catch {
    return "system";
  }
}

export function useTheme(): {
  mode: ThemeMode;
  theme: ResolvedTheme;
  setMode: (mode: ThemeMode) => void;
  toggle: () => void;
} {
  const [mode, setModeState] = useState<ThemeMode>(storedThemeMode);
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme);
  const theme = mode === "system" ? system : mode;

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!media) return;
    const onChange = () => setSystem(media.matches ? "dark" : "light");
    media.addEventListener?.("change", onChange);
    return () => media.removeEventListener?.("change", onChange);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(THEME_KEY, mode);
    } catch {
      // Keep the session preference even when persistence is unavailable.
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    meta?.setAttribute("content", theme === "dark" ? "#171614" : "#faf9f6");
  }, [mode, theme]);

  const setMode = useCallback((next: ThemeMode) => setModeState(next), []);
  const toggle = useCallback(() => {
    setModeState((current) => {
      const resolved = current === "system" ? systemTheme() : current;
      return resolved === "dark" ? "light" : "dark";
    });
  }, []);

  return { mode, theme, setMode, toggle };
}

export function useLanguage(): {
  preference: LanguagePreference;
  language: Language;
  setPreference: (preference: LanguagePreference) => void;
} {
  const [preference, setPreferenceState] = useState<LanguagePreference>(storedLanguagePreference);
  const language = useMemo(() => resolveLanguage(preference), [preference]);

  useEffect(() => {
    const onChange = () => {
      setPreferenceState((current) => (current === "system" ? current : current));
      setLanguagePreference(preference);
    };
    window.addEventListener("languagechange", onChange);
    return () => window.removeEventListener("languagechange", onChange);
  }, [preference]);

  const setPreference = useCallback((next: LanguagePreference) => {
    setPreferenceState(next);
    setLanguagePreference(next);
  }, []);

  return { preference, language, setPreference };
}
