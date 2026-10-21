import {
  Bot,
  Boxes,
  Database,
  Gauge,
  Info,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Meta, SettingsLogEntry } from "../api/types";
import type { LanguagePreference } from "../i18n";
import type { ThemeMode } from "../hooks/useTheme";
import { useSettings } from "../hooks/useSettings";
import { AboutSettings } from "./settings/AboutSettings";
import { AdvancedSettings } from "./settings/AdvancedSettings";
import { AgentsSettings } from "./settings/AgentsSettings";
import { BudgetSettings } from "./settings/BudgetSettings";
import { GeneralSettings } from "./settings/GeneralSettings";
import { PermissionsSettings } from "./settings/PermissionsSettings";
import { ProviderSettings } from "./settings/ProviderSettings";
import { WorkspaceSettings } from "./settings/WorkspaceSettings";
import { Button } from "./ui/Button";
import { Notice } from "./ui/Notice";

const SECTION_STORAGE = "codepilot.settingsSection";

const SECTIONS = [
  { id: "general", icon: SlidersHorizontal },
  { id: "models", icon: Boxes },
  { id: "agents", icon: Bot },
  { id: "permissions", icon: ShieldCheck },
  { id: "budget", icon: Gauge },
  { id: "workspace", icon: Database },
  { id: "advanced", icon: TerminalSquare },
  { id: "about", icon: Info },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

export function SettingsView({
  meta,
  apiKey,
  onSaveKey,
  onClearKey,
  themeMode,
  setThemeMode,
  languagePreference,
  setLanguagePreference,
  onSettingsSaved,
}: {
  meta: Meta | null;
  apiKey: string;
  onSaveKey: (key: string) => void;
  onClearKey: () => void;
  themeMode: ThemeMode;
  setThemeMode: (mode: ThemeMode) => void;
  languagePreference: LanguagePreference;
  setLanguagePreference: (preference: LanguagePreference) => void;
  onSettingsSaved: () => void;
}) {
  const { t } = useTranslation();
  const [section, setSection] = useState<SectionId>(() => {
    const stored = window.localStorage.getItem(SECTION_STORAGE);
    return SECTIONS.some((item) => item.id === stored) ? (stored as SectionId) : "general";
  });
  const [logs, setLogs] = useState<SettingsLogEntry[]>([]);
  const settings = useSettings(apiKey, onSettingsSaved);
  const { document, runtime, loading, busy, error, notice, setNotice } = settings;

  useEffect(() => {
    window.localStorage.setItem(SECTION_STORAGE, section);
  }, [section]);

  return (
    <div className="mx-auto w-full max-w-[1200px] px-5 py-7">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-[25px] font-semibold tracking-[-0.03em]">{t("settings.title")}</h1>
          <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-muted">{t("settings.subtitle")}</p>
          {document ? (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-subtle">
              <span className="rounded-full border bg-surface px-2 py-0.5 font-mono">
                {t("settings.version", { version: document.version })}
              </span>
              <span>
                {t("settings.secureStorage", {
                  state: document.secure_storage_available
                    ? t("settings.secureStorageAvailable")
                    : t("settings.secureStorageUnavailable"),
                })}
              </span>
              {!document.secure_storage_available ? (
                <span className="text-warning">{t("settings.secureStorageUnavailable")}</span>
              ) : null}
            </div>
          ) : null}
        </div>
        <Button
          size="sm"
          variant="ghost"
          icon={<RefreshCw size={13} />}
          onClick={() => void settings.refresh()}
          loading={loading}
        >
          {t("settings.reload")}
        </Button>
      </div>

      {loading ? (
        <div className="grid gap-3 xl:grid-cols-[210px_1fr]">
          <div className="skeleton h-64 w-full" />
          <div className="space-y-3">
            <div className="skeleton h-24 w-full" />
            <div className="skeleton h-40 w-full" />
          </div>
        </div>
      ) : error ? (
        <Notice tone="danger">{error}</Notice>
      ) : document ? (
        <>
          {notice ? (
            <div className="mb-3">
              <Notice
                tone={notice.tone}
                action={
                  <button className="text-[11px] underline" onClick={() => setNotice(null)}>
                    {t("common.dismiss")}
                  </button>
                }
              >
                {notice.text}
              </Notice>
            </div>
          ) : null}
          <div className="grid gap-4 xl:grid-cols-[210px_1fr]">
            <nav
              className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 scroll-thin xl:mx-0 xl:block xl:space-y-0.5 xl:overflow-visible xl:px-0"
              aria-label={t("settings.title")}
            >
              {SECTIONS.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    key={item.id}
                    className="nav-item shrink-0 whitespace-nowrap xl:w-full"
                    data-active={section === item.id}
                    onClick={() => setSection(item.id)}
                    aria-current={section === item.id ? "page" : undefined}
                  >
                    <Icon size={15} />
                    <span className="truncate">{t(`settings.sections.${item.id}`)}</span>
                  </button>
                );
              })}
              <div className="hidden pt-4 xl:block">
                <div className="rounded-lg border bg-canvas/40 px-2.5 py-2 text-[10px] leading-relaxed text-subtle">
                  {t("settings.scope.instant")} · {t("settings.scope.newTasks")} ·{" "}
                  {t("settings.scope.restart")}
                </div>
              </div>
            </nav>

            <div className="min-w-0">
              {section === "general" ? (
                <GeneralSettings
                  document={document}
                  apiKey={apiKey}
                  busy={busy}
                  execute={settings.execute}
                  themeMode={themeMode}
                  setThemeMode={setThemeMode}
                  languagePreference={languagePreference}
                  setLanguagePreference={setLanguagePreference}
                  onSaveKey={onSaveKey}
                  onClearKey={onClearKey}
                  notify={setNotice}
                />
              ) : null}
              {section === "models" ? (
                <ProviderSettings
                  document={document}
                  catalog={settings.catalog}
                  apiKey={apiKey}
                  busy={busy}
                  execute={settings.execute}
                  refresh={settings.refreshQuiet}
                  notify={setNotice}
                />
              ) : null}
              {section === "agents" ? (
                <AgentsSettings document={document} apiKey={apiKey} busy={busy} execute={settings.execute} />
              ) : null}
              {section === "permissions" ? <PermissionsSettings meta={meta} /> : null}
              {section === "budget" ? (
                <BudgetSettings document={document} apiKey={apiKey} busy={busy} execute={settings.execute} />
              ) : null}
              {section === "workspace" ? (
                <WorkspaceSettings
                  document={document}
                  apiKey={apiKey}
                  runtime={runtime}
                  busy={busy}
                  execute={settings.execute}
                  notify={setNotice}
                  refreshRuntime={settings.refreshRuntime}
                />
              ) : null}
              {section === "advanced" ? (
                <AdvancedSettings
                  document={document}
                  apiKey={apiKey}
                  runtime={runtime}
                  logs={logs}
                  onLogs={setLogs}
                  refreshRuntime={settings.refreshRuntime}
                  busy={busy}
                />
              ) : null}
              {section === "about" ? <AboutSettings meta={meta} runtime={runtime} apiKey={apiKey} /> : null}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
