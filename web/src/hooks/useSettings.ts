import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  getCatalog,
  getRuntimeFacts,
  getSettings,
} from "../api/client";
import type { CatalogTemplate, RuntimeFacts, SettingsDocument } from "../api/types";

export interface SettingsNotice {
  tone: "info" | "success" | "warning" | "danger";
  text: string;
}

export function useSettings(apiKey: string, onChanged?: () => void): {
  document: SettingsDocument | null;
  catalog: CatalogTemplate[];
  runtime: RuntimeFacts | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  notice: SettingsNotice | null;
  setNotice: (notice: SettingsNotice | null) => void;
  refresh: () => Promise<void>;
  refreshQuiet: () => Promise<void>;
  refreshRuntime: () => Promise<void>;
  adopt: (document: SettingsDocument, successText?: string) => void;
  execute: (action: () => Promise<SettingsDocument>, successText?: string) => Promise<SettingsDocument | null>;
} {
  const { t } = useTranslation();
  const [document, setDocument] = useState<SettingsDocument | null>(null);
  const [catalog, setCatalog] = useState<CatalogTemplate[]>([]);
  const [runtime, setRuntime] = useState<RuntimeFacts | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<SettingsNotice | null>(null);
  const [loadedKey, setLoadedKey] = useState(apiKey);

  // Reset the loading indicator synchronously during render when the API key
  // changes; the fetch itself runs in the effect below (setState after await).
  if (loadedKey !== apiKey) {
    setLoadedKey(apiKey);
    setLoading(true);
    setError(null);
  }

  const loadSettingsData = useCallback(async (): Promise<void> => {
    const [settings, templates] = await Promise.all([getSettings(apiKey), getCatalog(apiKey)]);
    setDocument(settings);
    setCatalog(templates);
  }, [apiKey]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await loadSettingsData();
      } catch (caught) {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : "Settings could not be loaded.");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadSettingsData]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await loadSettingsData();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Settings could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [loadSettingsData]);

  const refreshQuiet = useCallback(async () => {
    try {
      setDocument(await getSettings(apiKey));
    } catch (caught) {
      setNotice({
        tone: "danger",
        text: caught instanceof Error ? caught.message : "Settings could not be loaded.",
      });
    }
  }, [apiKey]);

  const refreshRuntime = useCallback(async () => {
    try {
      setRuntime(await getRuntimeFacts(apiKey));
    } catch (caught) {
      setNotice({
        tone: "danger",
        text: caught instanceof Error ? caught.message : "Runtime diagnostics are unavailable.",
      });
    }
  }, [apiKey]);

  const adopt = useCallback(
    (next: SettingsDocument, successText?: string) => {
      setDocument(next);
      onChanged?.();
      setNotice(successText ? { tone: "success", text: successText } : null);
    },
    [onChanged],
  );

  const execute = useCallback(
    async (action: () => Promise<SettingsDocument>, successText?: string) => {
      setBusy(true);
      try {
        const next = await action();
        adopt(next, successText);
        return next;
      } catch (caught) {
        const message =
          caught instanceof ApiError
            ? `${t(`apiErrors.${caught.code}`, { defaultValue: caught.message })}${caught.traceId ? ` · ${caught.traceId}` : ""}`
            : caught instanceof Error
              ? caught.message
              : "The settings request failed.";
        setNotice({ tone: "danger", text: message });
        if (caught instanceof ApiError && caught.status === 409) {
          void refresh();
        }
        return null;
      } finally {
        setBusy(false);
      }
    },
    [adopt, refresh, t],
  );

  return {
    document,
    catalog,
    runtime,
    loading,
    busy,
    error,
    notice,
    setNotice,
    refresh,
    refreshQuiet,
    refreshRuntime,
    adopt,
    execute,
  };
}
