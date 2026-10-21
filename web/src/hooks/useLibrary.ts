import { useCallback, useEffect, useRef, useState } from "react";

import { listHistory, listTasks } from "../api/client";
import type { HistoryItem, Task } from "../api/types";
import i18n from "../i18n";

export function useLibrary(apiKey: string): {
  tasks: Task[];
  history: HistoryItem[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
} {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    if (!apiKey) return;
    try {
      const [taskPage, historyItems] = await Promise.all([
        listTasks(apiKey, { limit: 30 }),
        listHistory(apiKey),
      ]);
      if (!mounted.current) return;
      setTasks(taskPage.items);
      setHistory(historyItems);
      setError(null);
    } catch (caught) {
      if (!mounted.current) return;
      setError(caught instanceof Error ? caught.message : i18n.t("errors.loadLibrary"));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [apiKey]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 20_000);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { tasks, history, loading, error, refresh };
}
