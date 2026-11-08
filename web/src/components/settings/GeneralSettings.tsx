import { Check, KeyRound, Languages, MonitorCog, Palette } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { updateSettingsSection } from "../../api/client";
import type { SettingsDocument } from "../../api/types";
import type { LanguagePreference } from "../../i18n";
import type { ThemeMode } from "../../hooks/useTheme";
import type { SettingsNotice } from "../../hooks/useSettings";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { Field, Segmented, SettingsSection, StatusPill } from "./shared";

type Execute = (
  action: () => Promise<SettingsDocument>,
  successText?: string,
) => Promise<SettingsDocument | null>;

export function GeneralSettings({
  document,
  apiKey,
  busy,
  execute,
  themeMode,
  setThemeMode,
  languagePreference,
  setLanguagePreference,
  onSaveKey,
  onClearKey,
  notify,
}: {
  document: SettingsDocument;
  apiKey: string;
  busy: boolean;
  execute: Execute;
  themeMode: ThemeMode;
  setThemeMode: (mode: ThemeMode) => void;
  languagePreference: LanguagePreference;
  setLanguagePreference: (preference: LanguagePreference) => void;
  onSaveKey: (key: string) => void;
  onClearKey: () => void;
  notify: (notice: SettingsNotice) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(document.general);
  const [draftKey, setDraftKey] = useState("");
  const [savedKey, setSavedKey] = useState(false);
  const [lastDocument, setLastDocument] = useState(document.general);

  // Re-seed the draft synchronously during render when the server document
  // changes (the sanctioned "adjust state when a prop changes" pattern).
  if (lastDocument !== document.general) {
    setLastDocument(document.general);
    setDraft(document.general);
  }

  const save = async (general = draft) => {
    window.localStorage.setItem("codepilot.density", general.activity_density);
    await execute(
      () => updateSettingsSection(apiKey, "general", { ...general }, document.version),
      t("settings.saved", { version: document.version + 1 }),
    );
  };

  const changeLanguage = async (preference: LanguagePreference) => {
    setLanguagePreference(preference);
    const next = { ...draft, language: preference };
    setDraft(next);
    await save(next);
  };

  const changeTheme = async (mode: ThemeMode) => {
    setThemeMode(mode);
    const next = { ...draft, theme: mode };
    setDraft(next);
    await save(next);
  };

  const saveApiKey = () => {
    if (draftKey.trim().length < 8) {
      notify({ tone: "danger", text: t("settings.general.keyTooShort") });
      return;
    }
    onSaveKey(draftKey.trim());
    setDraftKey("");
    setSavedKey(true);
    window.setTimeout(() => setSavedKey(false), 1600);
  };

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.general.language")}
        description={t("settings.general.languageHint")}
        scope="instant"
        actions={<Languages size={15} className="text-subtle" />}
      >
        <Field label={t("settings.general.language")}>
          <select
            className="select h-9 w-full max-w-xs text-[12px]"
            value={languagePreference}
            onChange={(event) => void changeLanguage(event.target.value as LanguagePreference)}
          >
            <option value="system">{t("settings.general.languageSystem")}</option>
            <option value="zh-CN">简体中文</option>
            <option value="en-US">English</option>
          </select>
        </Field>
      </SettingsSection>

      <SettingsSection
        title={t("settings.general.theme")}
        description={t("settings.general.themeHint")}
        scope="instant"
        actions={<Palette size={15} className="text-subtle" />}
      >
        <Segmented
          value={themeMode}
          onChange={(mode) => void changeTheme(mode)}
          ariaLabel={t("settings.general.theme")}
          options={[
            { value: "light", label: t("settings.general.themeLight") },
            { value: "dark", label: t("settings.general.themeDark") },
            { value: "system", label: t("settings.general.themeSystem") },
          ]}
        />
      </SettingsSection>

      <SettingsSection
        title={t("settings.general.defaultMode")}
        description={t("settings.general.defaultModeHint")}
        scope="newTasks"
        actions={<MonitorCog size={15} className="text-subtle" />}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("settings.general.defaultMode")}>
            <select
              className="select h-9 text-[12px]"
              value={draft.default_execution_mode}
              onChange={(event) =>
                setDraft({ ...draft, default_execution_mode: event.target.value as typeof draft.default_execution_mode })
              }
            >
              <option value="offline_fake">{t("composer.modeOffline")}</option>
              <option value="docker">{t("composer.modeDocker")}</option>
              <option value="live">{t("composer.modeLive")}</option>
            </select>
          </Field>
          <Field label={t("settings.general.density")}>
            <select
              className="select h-9 text-[12px]"
              value={draft.activity_density}
              onChange={(event) =>
                setDraft({ ...draft, activity_density: event.target.value as typeof draft.activity_density })
              }
            >
              <option value="summary">{t("settings.general.densitySummary")}</option>
              <option value="normal">{t("settings.general.densityNormal")}</option>
              <option value="detailed">{t("settings.general.densityDetailed")}</option>
            </select>
          </Field>
        </div>
        <div className="mt-3 flex justify-end">
          <Button size="sm" variant="primary" loading={busy} onClick={() => void save()}>
            {t("settings.save")}
          </Button>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.general.apiConnection")}
        description={t("settings.general.apiKeyStored")}
        actions={<KeyRound size={15} className="text-subtle" />}
      >
        <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px]">
          <StatusPill label={t("settings.general.keyPresent")} tone={apiKey ? "success" : "warning"} />
          <span className="truncate font-mono text-[10px] text-subtle">
            {t("settings.general.currentKey")}: {apiKey ? `${apiKey.slice(0, 3)}••••${apiKey.slice(-3)}` : t("settings.general.noKey")}
          </span>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <Field label={t("settings.general.replaceApiKey")}>
            <input
              className="input h-9 min-w-[240px] font-mono text-[11.5px]"
              type="password"
              value={draftKey}
              onChange={(event) => setDraftKey(event.target.value)}
              placeholder={t("settings.general.keyPlaceholder")}
              autoComplete="off"
            />
          </Field>
          <Button size="sm" onClick={saveApiKey} disabled={draftKey.trim().length < 8}>
            {t("settings.general.saveKey")}
          </Button>
          <Button size="sm" variant="ghost" onClick={onClearKey}>
            {t("settings.general.disconnect")}
          </Button>
        </div>
        {savedKey ? (
          <div className="mt-2 flex items-center gap-1 text-[10.5px] text-success">
            <Check size={11} /> {t("settings.general.keyReplaced")}
          </div>
        ) : null}
        <div className="mt-3">
          <Notice tone="info">
            {t("settings.general.apiKeyStored")} {t("connect.loopback")}
          </Notice>
        </div>
      </SettingsSection>
    </div>
  );
}
