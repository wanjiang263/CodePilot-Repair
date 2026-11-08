import { Loader2, Menu, Moon, Search, Sun, Wifi, WifiOff } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, getMeta } from "./api/client";
import type { Meta, Task } from "./api/types";
import { CommandPalette } from "./components/CommandPalette";
import { Composer } from "./components/Composer";
import { ConnectScreen } from "./components/ConnectScreen";
import { SessionWorkspace } from "./components/SessionWorkspace";
import { Sidebar } from "./components/Sidebar";
import { Notice } from "./components/ui/Notice";
import { useApiKey } from "./hooks/useApiKey";
import { useLanguage, useTheme } from "./hooks/useTheme";
import { useLibrary } from "./hooks/useLibrary";
import { useMediaQuery } from "./hooks/useMediaQuery";
import { parseView, toHash, type View } from "./lib/navigation";

const HistoryView = lazy(() =>
  import("./components/HistoryView").then((module) => ({ default: module.HistoryView })),
);
const BenchmarksView = lazy(() =>
  import("./components/BenchmarksView").then((module) => ({ default: module.BenchmarksView })),
);
const BenchmarkDetailView = lazy(() =>
  import("./components/BenchmarkDetailView").then((module) => ({
    default: module.BenchmarkDetailView,
  })),
);
const SettingsView = lazy(() =>
  import("./components/SettingsView").then((module) => ({ default: module.SettingsView })),
);

function ViewLoading() {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center gap-2 text-[12px] text-muted">
      <Loader2 size={14} className="animate-spin text-accent" />
      {t("app.loadingView")}
    </div>
  );
}

function currentView(): View {
  return parseView(window.location.hash);
}

function navigate(view: View) {
  const next = toHash(view);
  if (window.location.hash !== next) {
    window.location.hash = next;
  }
}

export default function App() {
  const { t } = useTranslation();
  const { apiKey, setApiKey, clearApiKey } = useApiKey();
  const { mode: themeMode, theme, toggle: toggleTheme, setMode: setThemeMode } = useTheme();
  const { preference: languagePreference, setPreference: setLanguagePreference } = useLanguage();
  const [view, setView] = useState<View>(currentView);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem("codepilot.sidebar") === "collapsed",
  );
  const [mobileSidebar, setMobileSidebar] = useState(false);
  const [detailOpen, setDetailOpen] = useState(true);
  const isLarge = useMediaQuery("(min-width: 1024px)");
  const overlayDetail = useMediaQuery("(max-width: 1279px)");
  const library = useLibrary(apiKey);

  const pageTitle = useCallback(
    (target: View): string => {
      switch (target.name) {
        case "history":
          return t("app.history");
        case "benchmarks":
          return t("app.benchmarks");
        case "benchmark":
          return t("app.benchmark");
        case "settings":
          return t("app.settings");
        default:
          return t("app.newRepair");
      }
    },
    [t],
  );

  const loadMeta = useCallback(
    async (key: string, signal?: AbortSignal) => {
      setMetaLoading(true);
      setMetaError(null);
      try {
        const value = await getMeta(key, signal);
        setMeta(value);
        setAuthError(null);
      } catch (caught) {
        if (signal?.aborted) return;
        if (caught instanceof ApiError && caught.status === 401) {
          setAuthError(t("app.storedKeyRejected"));
        } else {
          setMetaError(caught instanceof Error ? caught.message : t("app.apiUnreachable"));
        }
      } finally {
        if (!signal?.aborted) setMetaLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    const onHash = () => setView(currentView());
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) window.location.hash = "#/new";
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    window.localStorage.setItem("codepilot.sidebar", sidebarCollapsed ? "collapsed" : "expanded");
  }, [sidebarCollapsed]);

  const [lastApiKey, setLastApiKey] = useState(apiKey);

  // Clear cached metadata synchronously during render when the API key is
  // removed (the sanctioned "adjust state when a prop changes" pattern).
  if (lastApiKey !== apiKey) {
    setLastApiKey(apiKey);
    if (!apiKey) {
      setMeta(null);
      setAuthError(null);
    }
  }

  useEffect(() => {
    if (!apiKey) return;
    const controller = new AbortController();
    (async () => {
      await loadMeta(apiKey, controller.signal);
    })();
    return () => controller.abort();
  }, [apiKey, loadMeta]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      }
      if (modifier && event.key.toLowerCase() === "b") {
        event.preventDefault();
        if (isLarge) setSidebarCollapsed((value) => !value);
        else setMobileSidebar((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isLarge]);

  const connected = Boolean(meta) && !metaError;

  const go = useCallback((next: View) => {
    navigate(next);
    setView(next);
    setMobileSidebar(false);
  }, []);

  const connect = useCallback(
    async (candidate: string) => {
      const value = await getMeta(candidate);
      setMeta(value);
      setMetaError(null);
      setAuthError(null);
      setApiKey(candidate);
    },
    [setApiKey],
  );

  const onCreated = useCallback(
    (task: Task) => {
      void library.refresh();
      go({ name: "session", taskId: task.task_id });
    },
    [go, library],
  );

  const onSessionChange = useCallback(() => {
    void library.refresh();
  }, [library]);

  const refreshMeta = useCallback(() => {
    if (apiKey) void loadMeta(apiKey);
    void library.refresh();
  }, [apiKey, library, loadMeta]);

  if (!apiKey || authError) {
    return (
      <ConnectScreen
        onConnect={connect}
        initialError={authError}
      />
    );
  }

  const sidebar = (
    <Sidebar
      view={view}
      onNavigate={go}
      tasks={library.tasks}
      history={library.history}
      collapsed={isLarge ? sidebarCollapsed : false}
      onToggle={() => {
        if (isLarge) setSidebarCollapsed((value) => !value);
        else setMobileSidebar(false);
      }}
      theme={theme}
      onToggleTheme={toggleTheme}
      onSearch={() => setPaletteOpen(true)}
      connected={connected}
    />
  );

  return (
    <div className="flex h-screen overflow-hidden bg-canvas">
      {isLarge ? (
        sidebar
      ) : mobileSidebar ? (
        <>
          <div className="fixed inset-0 z-40 overlay-scrim" onClick={() => setMobileSidebar(false)} aria-hidden />
          <div className="fixed inset-y-0 left-0 z-50 h-full">{sidebar}</div>
        </>
      ) : null}

      <main className="relative flex min-w-0 flex-1 flex-col">
        {view.name !== "session" && view.name !== "replay" ? (
          <div className="flex h-12 shrink-0 items-center gap-2 border-b bg-canvas/94 px-4 backdrop-blur">
            {!isLarge ? (
              <button className="icon-btn" onClick={() => setMobileSidebar(true)} aria-label={t("app.openNavigation")}>
                <Menu size={16} />
              </button>
            ) : null}
            <span className="text-[12px] font-medium">{pageTitle(view)}</span>
            <div className="ml-auto flex items-center gap-1">
              <button className="icon-btn" onClick={() => setPaletteOpen(true)} title={t("app.searchTitle")} aria-label={t("app.search")}>
                <Search size={15} />
              </button>
              <button
                className="icon-btn"
                onClick={toggleTheme}
                title={theme === "dark" ? t("app.lightTheme") : t("app.darkTheme")}
                aria-label={theme === "dark" ? t("app.lightTheme") : t("app.darkTheme")}
              >
                {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
              </button>
              <span
                className="ml-1 inline-flex items-center gap-1 text-[10px] text-subtle"
                title={connected ? t("app.apiConnected") : metaError ?? t("app.connecting")}
              >
                {connected ? <Wifi size={12} className="text-success" /> : <WifiOff size={12} className="text-warning" />}
                {metaLoading ? t("app.connecting") : connected ? t("app.local") : t("app.offline")}
              </span>
            </div>
          </div>
        ) : null}

        {metaError ? (
          <div className="px-4 pt-3">
            <Notice tone="warning">{metaError}</Notice>
          </div>
        ) : null}

        <Suspense fallback={<ViewLoading />}>
        {view.name === "new" ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <Composer meta={meta} apiKey={apiKey} onCreated={onCreated} />
          </div>
        ) : null}
        {view.name === "session" ? (
          <SessionWorkspace
            key={`live-${view.taskId}`}
            mode="live"
            taskId={view.taskId}
            apiKey={apiKey}
            onSessionChange={onSessionChange}
            onToggleSidebar={() => setMobileSidebar(true)}
            globalDetailOpen={detailOpen}
            onGlobalDetailOpen={setDetailOpen}
            overlay={overlayDetail}
            onBack={() => go({ name: "history" })}
          />
        ) : null}
        {view.name === "replay" ? (
          <SessionWorkspace
            key={`replay-${view.benchId}`}
            mode="replay"
            benchId={view.benchId}
            apiKey={apiKey}
            onSessionChange={onSessionChange}
            onToggleSidebar={() => setMobileSidebar(true)}
            globalDetailOpen={detailOpen}
            onGlobalDetailOpen={setDetailOpen}
            overlay={overlayDetail}
            onBack={() => go({ name: "history" })}
          />
        ) : null}
        {view.name === "history" ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <HistoryView
              history={library.history}
              loading={library.loading}
              onOpenReplay={(benchId) => go({ name: "replay", benchId })}
            />
          </div>
        ) : null}
        {view.name === "benchmarks" ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <BenchmarksView
              benchmarks={library.history}
              loading={library.loading}
              onOpenBenchmark={(benchId) => go({ name: "benchmark", benchId })}
            />
          </div>
        ) : null}
        {view.name === "benchmark" ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <BenchmarkDetailView
              benchId={view.benchId}
              apiKey={apiKey}
              onBack={() => go({ name: "benchmarks" })}
              onOpenReplay={(benchId) => go({ name: "replay", benchId })}
            />
          </div>
        ) : null}
        {view.name === "settings" ? (
          <div className="min-h-0 flex-1 overflow-y-auto scroll-thin">
            <SettingsView
              meta={meta}
              apiKey={apiKey}
              onSaveKey={(key) => {
                setApiKey(key);
                setAuthError(null);
              }}
              onClearKey={clearApiKey}
              themeMode={themeMode}
              setThemeMode={setThemeMode}
              languagePreference={languagePreference}
              setLanguagePreference={setLanguagePreference}
              onSettingsSaved={refreshMeta}
            />
          </div>
        ) : null}
        </Suspense>
      </main>

      <CommandPalette
        open={paletteOpen}
        tasks={library.tasks}
        history={library.history}
        onClose={() => setPaletteOpen(false)}
        onSelectTask={(taskId) => go({ name: "session", taskId })}
        onSelectReplay={(benchId) => go({ name: "replay", benchId })}
      />
      <div className="sr-only" aria-live="polite">
        {metaLoading ? t("app.connectingToApi") : connected ? t("app.live") : t("app.unavailable")}
      </div>
    </div>
  );
}
