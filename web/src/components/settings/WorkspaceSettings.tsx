import { Database, Download, HardDrive, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  cleanupTasks,
  exportSettings,
  importSettings,
  listTasks,
  updateSettingsSection,
} from "../../api/client";
import type { RuntimeFacts, SettingsDocument, Task } from "../../api/types";
import type { SettingsNotice } from "../../hooks/useSettings";
import { formatNumber } from "../../lib/format";
import { isTerminal } from "../../lib/status";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { EmptyCard, Field, SettingsSection } from "./shared";

type Execute = (
  action: () => Promise<SettingsDocument>,
  successText?: string,
) => Promise<SettingsDocument | null>;

export function WorkspaceSettings({
  document,
  apiKey,
  runtime,
  busy,
  execute,
  notify,
  refreshRuntime,
}: {
  document: SettingsDocument;
  apiKey: string;
  runtime: RuntimeFacts | null;
  busy: boolean;
  execute: Execute;
  notify: (notice: SettingsNotice) => void;
  refreshRuntime: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState(document.workspace.default_repository_kind);
  const [repository, setRepository] = useState(document.workspace.default_repository);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmation, setConfirmation] = useState("");
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [lastWorkspace, setLastWorkspace] = useState(document.workspace);

  // Re-seed the repository defaults synchronously during render when the
  // server document changes (the sanctioned "adjust state" pattern).
  if (lastWorkspace !== document.workspace) {
    setLastWorkspace(document.workspace);
    setKind(document.workspace.default_repository_kind);
    setRepository(document.workspace.default_repository);
  }

  useEffect(() => {
    let mounted = true;
    listTasks(apiKey, { limit: 100 })
      .then((page) => {
        if (mounted) setTasks(page.items.filter((task) => isTerminal(task.ui_status)));
      })
      .catch(() => undefined);
    return () => {
      mounted = false;
    };
  }, [apiKey]);

  const saveWorkspace = async () => {
    await execute(
      () =>
        updateSettingsSection(
          apiKey,
          "workspace",
          { default_repository_kind: kind, default_repository: repository.trim() },
          document.version,
        ),
      t("settings.saved", { version: document.version + 1 }),
    );
  };

  const download = async () => {
    try {
      const payload = await exportSettings(apiKey);
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = "codepilot-settings.json";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      notify({ tone: "danger", text: caught instanceof Error ? caught.message : t("settings.saveFailed") });
    }
  };

  const importFile = async (file: File) => {
    try {
      const payload = JSON.parse(await file.text()) as Record<string, unknown>;
      const next = await execute(
        () => importSettings(apiKey, payload, document.version),
        t("settings.workspace.imported", { version: document.version + 1 }),
      );
      if (next) await refreshRuntime();
    } catch (caught) {
      notify({ tone: "danger", text: caught instanceof Error ? caught.message : t("settings.workspace.importFailed") });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const runCleanup = async () => {
    setCleanupBusy(true);
    try {
      const result = await cleanupTasks(apiKey, selected, document.version);
      const skipped = result.skipped.length
        ? ` ${t("settings.workspace.cleanupSkipped", { count: result.skipped.length })}`
        : "";
      notify({
        tone: result.deleted.length ? "success" : "warning",
        text: t("settings.workspace.cleanupDone", { count: result.deleted.length }) + skipped,
      });
      setSelected([]);
      setConfirmation("");
      setTasks((current) => current.filter((task) => !result.deleted.includes(task.task_id)));
      await refreshRuntime();
    } catch (caught) {
      notify({ tone: "danger", text: caught instanceof Error ? caught.message : t("settings.saveFailed") });
    } finally {
      setCleanupBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.workspace.title")}
        description={t("settings.workspace.intro")}
        scope="newTasks"
        actions={<HardDrive size={15} className="text-subtle" />}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("settings.workspace.defaultKind")}>
            <select
              className="select h-9 text-[12px]"
              value={kind}
              onChange={(event) => setKind(event.target.value as "demo" | "https")}
            >
              <option value="demo">{t("settings.workspace.demoRepository")}</option>
              <option value="https">{t("settings.workspace.httpsRepository")}</option>
            </select>
          </Field>
          <Field label={t("settings.workspace.defaultRepository")} hint={t("settings.general.workspaceDefaultHint")}>
            <input
              className="input h-9 text-[12px]"
              value={repository}
              onChange={(event) => setRepository(event.target.value)}
              placeholder={kind === "demo" ? "mini_calc" : "https://github.com/owner/repository"}
            />
          </Field>
        </div>
        <div className="mt-3 flex justify-end">
          <Button size="sm" variant="primary" loading={busy} onClick={() => void saveWorkspace()}>
            {t("settings.save")}
          </Button>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.workspace.storage")}
        description={t("settings.workspace.storageRoot", { path: runtime?.storage.artifact_root ?? "artifacts/runs" })}
        scope="readOnly"
        actions={<Database size={15} className="text-subtle" />}
      >
        <div className="flex flex-wrap items-center gap-3 text-[12px]">
          <span className="rounded-lg border bg-canvas px-3 py-2 font-mono">
            {formatNumber(runtime?.storage.artifact_bytes ?? 0)} B
          </span>
          <span className="text-subtle">
            {t("settings.workspace.storageValue", {
              size: `${formatNumber(runtime?.storage.artifact_bytes ?? 0)} B`,
              files: runtime?.storage.artifact_files ?? 0,
            })}
          </span>
          <span className="text-subtle">
            {t("settings.workspace.historyValue")}
          </span>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.workspace.export")}
        description={t("settings.workspace.exportHint")}
        actions={<Download size={15} className="text-subtle" />}
      >
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => void download()}>
            {t("settings.workspace.exportAction")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={<Upload size={13} />}
            onClick={() => fileRef.current?.click()}
          >
            {t("settings.workspace.importAction")}
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importFile(file);
            }}
          />
        </div>
        <p className="mt-2 text-[10.5px] leading-relaxed text-subtle">{t("settings.workspace.importHint")}</p>
      </SettingsSection>

      <SettingsSection
        title={t("settings.workspace.cleanup")}
        description={t("settings.workspace.cleanupHint")}
        actions={<Trash2 size={15} className="text-subtle" />}
      >
        {!tasks.length ? (
          <EmptyCard>{t("settings.workspace.cleanupNone")}</EmptyCard>
        ) : (
          <>
            <div className="mb-2 text-[10.5px] text-subtle">{t("settings.workspace.cleanupSelect")}</div>
            <div className="max-h-56 space-y-1 overflow-y-auto scroll-thin rounded-lg border bg-canvas/40 p-2">
              {tasks.map((task) => (
                <label key={task.task_id} className="flex items-center gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-elevated">
                  <input
                    type="checkbox"
                    checked={selected.includes(task.task_id)}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? [...selected, task.task_id]
                          : selected.filter((item) => item !== task.task_id),
                      )
                    }
                  />
                  <span className="min-w-0 flex-1 truncate">{task.issue_preview}</span>
                  <span className="font-mono text-[9px] text-subtle">{task.task_id.slice(0, 14)}</span>
                </label>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <Field label={t("settings.workspace.cleanupConfirm")}>
                <input
                  className="input h-8 w-40 font-mono text-[11.5px]"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder="DELETE"
                />
              </Field>
              <Button
                size="sm"
                variant="danger"
                icon={<Trash2 size={12} />}
                loading={cleanupBusy}
                disabled={!selected.length || confirmation !== "DELETE"}
                onClick={() => void runCleanup()}
              >
                {t("settings.workspace.cleanupAction")}
              </Button>
            </div>
          </>
        )}
        <div className="mt-3">
          <Notice tone="warning">{t("settings.workspace.cleanupHint")}</Notice>
        </div>
      </SettingsSection>
    </div>
  );
}
