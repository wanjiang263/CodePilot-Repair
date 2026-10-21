import { LockKeyhole, Network, ShieldCheck, TerminalSquare } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Meta } from "../../api/types";
import { Notice } from "../ui/Notice";
import { SettingsSection, StatusPill } from "./shared";

export function PermissionsSettings({ meta }: { meta: Meta | null }) {
  const { t } = useTranslation();
  if (!meta) return null;
  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.permissions.repositoryHosts")}
        description={t("settings.permissions.repositoryHostsHint")}
        scope="readOnly"
        actions={<StatusPill label={t("settings.permissions.adminOnly")} tone="neutral" />}
      >
        <div className="flex flex-wrap gap-1.5">
          {meta.allowed_repository_hosts.map((host) => (
            <span key={host} className="rounded border bg-canvas px-2 py-1 font-mono text-[10.5px] text-muted">
              {host}
            </span>
          ))}
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.permissions.executionBackend")}
        description={t("settings.permissions.executionBackendHint")}
        scope="readOnly"
        actions={<TerminalSquare size={15} className="text-subtle" />}
      >
        <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
          <span className="rounded border bg-surface px-2 py-1 font-mono">{meta.sandbox.execution_backend}</span>
          <StatusPill
            label={
              meta.sandbox.execution_backend === "docker"
                ? t("settings.permissions.liveSandboxRequirement")
                : t("settings.permissions.sandboxLimitsValue")
            }
            tone={meta.sandbox.execution_backend === "docker" ? "success" : "warning"}
          />
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.permissions.networkPolicy")}
        description={t("settings.permissions.networkPolicyHint")}
        scope="readOnly"
        actions={<Network size={15} className="text-subtle" />}
      >
        <div className="text-[11.5px]">
          <span className="rounded border bg-surface px-2 py-1 font-mono">
            {t("settings.permissions.networkPolicyValue")}
          </span>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.permissions.toolAllowlist")}
        description={t("settings.permissions.toolAllowlistHint")}
        scope="readOnly"
        actions={<LockKeyhole size={15} className="text-subtle" />}
      >
        <div className="flex flex-wrap gap-1.5">
          {meta.sandbox.tools.map((tool) => (
            <span key={tool} className="rounded border bg-canvas px-1.5 py-0.5 font-mono text-[10px] text-muted">
              {tool}
            </span>
          ))}
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.permissions.workspaceBoundary")}
        description={t("settings.permissions.workspaceBoundaryValue")}
        scope="readOnly"
        actions={<ShieldCheck size={15} className="text-subtle" />}
      >
        <div className="space-y-2 text-[11.5px] leading-relaxed text-muted">
          <p>{t("settings.permissions.dangerousActionsValue")}</p>
          <p>{t("settings.permissions.sandboxLimitsValue")}</p>
          <Notice tone="warning">{t("settings.permissions.liveSandboxRequirement")}</Notice>
        </div>
      </SettingsSection>
    </div>
  );
}
