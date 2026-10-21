import { useCallback, useState } from "react";

const STORAGE_KEY = "codepilot.apiKey";

function initialKey(): string {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function useApiKey(): {
  apiKey: string;
  setApiKey: (value: string) => void;
  clearApiKey: () => void;
} {
  const [apiKey, setKeyState] = useState<string>(initialKey);

  const setApiKey = useCallback((value: string) => {
    const trimmed = value.trim();
    try {
      if (trimmed) window.sessionStorage.setItem(STORAGE_KEY, trimmed);
      else window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Session storage can be disabled; the in-memory key still works for this tab.
    }
    setKeyState(trimmed);
  }, []);

  const clearApiKey = useCallback(() => setApiKey(""), [setApiKey]);

  return { apiKey, setApiKey, clearApiKey };
}
