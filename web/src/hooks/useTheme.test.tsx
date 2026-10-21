import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTheme } from "./useTheme";

describe("useTheme", () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.classList.remove("dark");
    document.documentElement.dataset.theme = "";
  });

  it("applies light, dark and persisted explicit modes", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setMode("dark"));
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("codepilot.theme")).toBe("dark");

    act(() => result.current.setMode("light"));
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem("codepilot.theme")).toBe("light");
  });

  it("toggles away from the resolved system theme", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setMode("system"));
    const resolved = result.current.theme;
    act(() => result.current.toggle());
    expect(result.current.mode).toBe(resolved === "dark" ? "light" : "dark");
  });

  it("follows system theme changes while in system mode", () => {
    let listener: (() => void) | null = null;
    const media = {
      matches: false,
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addEventListener: vi.fn((_event: string, callback: () => void) => {
        listener = callback;
      }),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    };
    const original = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockReturnValue(media),
    });
    try {
      const { result } = renderHook(() => useTheme());
      act(() => result.current.setMode("system"));
      expect(result.current.theme).toBe("light");
      act(() => {
        media.matches = true;
        listener?.();
      });
      expect(result.current.theme).toBe("dark");
    } finally {
      Object.defineProperty(window, "matchMedia", { writable: true, value: original });
    }
  });
});
