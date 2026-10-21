import { Activity, RefreshCw, ServerCog, ShieldCheck } from "lucide-react";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { getSettingsLogs } from "../../api/client";
import type { RuntimeFacts, SettingsDocument, SettingsLogEntry } from "../../api/types";
import { Button } from "../ui/Button";
import { EmptyCard, SettingsSection, StatusPill } from "./shared";

export function AdvancedSettings({
  document,
  apiKey,
  runtime,
  logs,
  onLogs,
  refreshRuntime,
  busy,
}: {
  document: SettingsDocument;
  apiKey: string;
  runtime: RuntimeFacts | null;
  logs: SettingsLogEntry[];
  onLogs: (logs: SettingsLogEntry[]) => void;
  refreshRuntime: () => Promise<void>;
  busy: boolean;
}) {
  const { t } = useTranslation();
  useEffect(() => {
    if (runtime) return;
    void refreshRuntime();
  }, [runtime, refreshRuntime]);

  useEffect(() => {
    let mounted = true;
    getSettingsLogs(apiKey)
      .then((items) => {
        if (mounted) onLogs(items);
      })
      .catch(() => undefined);
    return () => {
      mounted = false;
    };
  }, [apiKey, document.version, onLogs]);

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.advanced.title")}
        description={t("settings.advanced.intro")}
        scope="readOnly"
        actions={
          <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} loading={busy} onClick={() => void refreshRuntime()}>
            {t("settings.advanced.refresh")}
          </Button>
        }
      >
        {!runtime ? (
          <div className="skeleton h-28 w-full" />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.1em] text-subtle">
                <ServerCog size={12} /> {t("settings.advanced.worker")}
              </div>
              <div className="mt-1 text-[12px]">
                {t("settings.advanced.workerQueued", { count: runtime.worker.queued })} ·{" "}
                {t("settings.advanced.workerRunning", { count: runtime.worker.running })}
              </div>
              <div className="mt-0.5 truncate text-[10px] text-subtle">
                {t("settings.advanced.lastWorker", { id: runtime.worker.last_worker ?? "—" })}
              </div>
            </div>
            <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.1em] text-subtle">
                <Activity size={12} /> {t("settings.advanced.docker")}
              </div>
              <div className="mt-1">
                <StatusPill
                  label={
                    runtime.docker.available
                      ? t("settings.advanced.dockerAvailable", { version: runtime.docker.server_version ?? "?" })
                      : t("settings.advanced.dockerUnavailable")
                  }
                  tone={runtime.docker.available ? "success" : "warning"}
                />
              </div>
              <div className="mt-1 text-[10px] text-subtle">
                {t("settings.advanced.backend", { backend: runtime.worker.execution_backend })}
              </div>
            </div>
            <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
              <div className="text-[10px] uppercase tracking-[0.1em] text-subtle">{t("settings.advanced.eventSink")}</div>
              <div className="mt-1 font-mono text-[11px]">{runtime.event_sink.kind}</div>
              <div className="text-[9.5px] text-subtle">{runtime.event_sink.status}</div>
            </div>
            <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
              <div className="text-[10px] uppercase tracking-[0.1em] text-subtle">{t("settings.advanced.checkpointer")}</div>
              <div className="mt-1 font-mono text-[11px]">{runtime.checkpointer.kind}</div>
              <div className="text-[9.5px] text-subtle">{runtime.checkpointer.scope}</div>
            </div>
          </div>
        )}
        {runtime ? (
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[10.5px] text-subtle">
            <span>{t("settings.advanced.productVersion")}: {runtime.version}</span>
            <span>{t("settings.advanced.gitCommit")}: {runtime.git_commit ?? "—"}</span>
            <span>{t("settings.advanced.settingsVersion")}: {runtime.settings_version}</span>
            <span>
              {t("settings.advanced.secureStorage")}:{" "}
              {runtime.secure_storage_available ? t("settings.secureStorageAvailable") : t("settings.secureStorageUnavailable")}
            </span>
          </div>
        ) : null}
      </SettingsSection>

      <SettingsSection title={t("settings.advanced.providers")} scope="readOnly">
        {!runtime || !runtime.providers.length ? (
          <EmptyCard>{t("settings.models.noProvidersDescription")}</EmptyCard>
        ) : (
          <div className="space-y-2">
            {runtime.providers.map((diagnostic, index) => (
              <details key={index} className="rounded-lg border bg-surface px-3 py-2">
                <summary className="cursor-pointer text-[11.5px]">
                  <span className="font-mono">{document.providers[index]?.provider_id ?? `provider-${index + 1}`}</span>
                  <span className="ml-2">
                    <StatusPill
                      label={diagnostic.ok ? t("settings.models.configOk") : t("settings.models.configInvalid")}
                      tone={diagnostic.ok ? "success" : "danger"}
                    />
                  </span>
                </summary>
                <div className="mt-2 space-y-1">
                  {diagnostic.checks.map((check) => (
                    <div key={`${check.name}-${check.detail}`} className="flex items-start gap-2 text-[10.5px]">
                      <span className={check.ok ? "text-success" : "text-warning"}>
                        {check.ok ? "✓" : "!"}
                      </span>
                      <span className="font-mono">{check.name}</span>
                      <span className="min-w-0 flex-1 text-subtle">{check.detail}</span>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        title={t("settings.advanced.logs")}
        description={t("settings.advanced.logsHint")}
        scope="readOnly"
      >
        {!logs.length ? (
          <EmptyCard>{t("common.empty")}</EmptyCard>
        ) : (
          <div className="max-h-60 space-y-1 overflow-y-auto scroll-thin rounded-lg border bg-terminal-surface px-3 py-2 font-mono text-[10px]">
            {logs.map((entry) => (
              <div key={`${entry.seq}-${entry.timestamp}`} className="flex gap-2">
                <span className="shrink-0 text-subtle">{entry.timestamp?.slice(11, 19) ?? "—"}</span>
                <span className="shrink-0 text-accent">{entry.node}</span>
                <span className="shrink-0 text-muted">{entry.type}</span>
                <span className="min-w-0 flex-1 truncate">{entry.summary ?? entry.status ?? ""}</span>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>

      <SettingsSection title={t("settings.advanced.audit")} scope="readOnly" actions={<ShieldCheck size={15} className="text-subtle" />}>
        {!runtime?.audit.length ? (
          <EmptyCard>{t("settings.advanced.auditEmpty")}</EmptyCard>
        ) : (
          <div className="space-y-1">
            {runtime.audit.map((entry) => (
              <div key={`${entry.created_at}-${entry.action}`} className="flex flex-wrap items-baseline gap-2 text-[10.5px]">
                <span className="font-mono text-subtle">{entry.created_at.slice(0, 19)}</span>
                <span className="font-mono text-muted">{entry.action}</span>
                <span className="min-w-0 flex-1 truncate text-subtle">{entry.summary}</span>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>
    </div>
  );
}
