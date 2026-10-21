import { Bot, CheckCircle2, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { updateSettingsSection } from "../../api/client";
import type { AgentSettings as AgentSettingsShape, SettingsDocument } from "../../api/types";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { EmptyCard, Field, SettingsSection, StatusPill } from "./shared";

type Execute = (
  action: () => Promise<SettingsDocument>,
  successText?: string,
) => Promise<SettingsDocument | null>;

const ROLES = ["localizer", "planner", "coder", "reproducer", "diagnoser", "reviewer"] as const;

export function AgentsSettings({
  document,
  apiKey,
  busy,
  execute,
}: {
  document: SettingsDocument;
  apiKey: string;
  busy: boolean;
  execute: Execute;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<AgentSettingsShape>(document.agents);

  useEffect(() => {
    setDraft(document.agents);
  }, [document.agents]);

  const options = useMemo(
    () =>
      document.providers.flatMap((provider) =>
        provider.models.map((model) => ({
          key: `${provider.provider_id}/${model.model_id}`,
          providerId: provider.provider_id,
          modelId: model.model_id,
          label: `${provider.name} · ${model.display_name || model.model_id}`,
          model,
          provider,
        })),
      ),
    [document.providers],
  );

  const defaultKey =
    draft.default_provider_id && draft.default_model_id
      ? `${draft.default_provider_id}/${draft.default_model_id}`
      : "";

  const setDefault = (key: string) => {
    if (!key) {
      setDraft({ ...draft, default_provider_id: null, default_model_id: null });
      return;
    }
    const option = options.find((item) => item.key === key);
    if (!option) return;
    setDraft({ ...draft, default_provider_id: option.providerId, default_model_id: option.modelId });
  };

  const setRole = (role: string, key: string) => {
    const roles = { ...draft.roles };
    if (!key) {
      delete roles[role];
    } else {
      const option = options.find((item) => item.key === key);
      if (!option) return;
      roles[role] = {
        provider_id: option.providerId,
        model_id: option.modelId,
        reasoning: roles[role]?.reasoning ?? "inherit",
      };
    }
    setDraft({ ...draft, roles });
  };

  const setReasoning = (role: string, reasoning: "inherit" | "disabled") => {
    const current = draft.roles[role];
    if (!current) return;
    setDraft({
      ...draft,
      roles: { ...draft.roles, [role]: { ...current, reasoning } },
    });
  };

  const save = async () => {
    await execute(
      () => updateSettingsSection(apiKey, "agents", { ...draft }, document.version),
      t("settings.saved", { version: document.version + 1 }),
    );
  };

  const selectedModel = (providerId: string | null, modelId: string | null) =>
    options.find((item) => item.providerId === providerId && item.modelId === modelId);

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.agents.defaultModel")}
        description={t("settings.agents.defaultHint")}
        scope="newTasks"
        actions={<Bot size={15} className="text-subtle" />}
      >
        {!options.length ? (
          <EmptyCard>{t("settings.agents.missingModel")}</EmptyCard>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.agents.model")} hint={t("settings.agents.defaultHint")} wide>
              <select
                className="select h-9 text-[12px]"
                value={defaultKey}
                onChange={(event) => setDefault(event.target.value)}
              >
                <option value="">{t("settings.agents.noDefault")}</option>
                {options.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
            </Field>
            {selectedModel(draft.default_provider_id, draft.default_model_id) ? (
              <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
                {selectedModel(draft.default_provider_id, draft.default_model_id)?.model.routing_ready ? (
                  <StatusPill label={t("settings.models.readyForLive")} tone="success" />
                ) : (
                  <StatusPill label={t("settings.models.incompleteSpecs")} tone="warning" />
                )}
                {selectedModel(draft.default_provider_id, draft.default_model_id)?.model.blockers.map((blocker) => (
                  <span key={blocker} className="rounded border bg-canvas px-1.5 py-0.5 text-[9.5px] text-warning">
                    {blocker}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        )}
      </SettingsSection>

      <SettingsSection
        title={t("settings.agents.rolesTitle")}
        description={t("settings.agents.intro")}
        scope="newTasks"
      >
        <div className="space-y-2">
          {ROLES.map((role) => {
            const route = draft.roles[role];
            const configured = Boolean(route?.provider_id && route?.model_id);
            const option = configured
              ? selectedModel(route.provider_id, route.model_id)
              : selectedModel(draft.default_provider_id, draft.default_model_id);
            const supportsThinking = option?.model.supports_thinking === true;            return (
              <div key={role} className="grid gap-2 rounded-xl border bg-surface px-3 py-3 md:grid-cols-[150px_1fr_180px]">
                <div>
                  <div className="text-[12px] font-medium">{t(`settings.agents.roles.${role}`)}</div>
                  <div className="mt-0.5 text-[10px] leading-relaxed text-subtle">
                    {t(`settings.agents.roleHint.${role}`)}
                  </div>
                </div>
                <label className="block">
                  <span className="mb-1 block text-[10px] text-subtle">{t("settings.agents.override")}</span>
                  <select
                    className="select h-8 text-[11.5px]"
                    value={configured ? `${route.provider_id}/${route.model_id}` : ""}
                    onChange={(event) => setRole(role, event.target.value)}
                  >
                    <option value="">
                      {t("settings.agents.inheritDefault", {
                        model:
                          defaultKey
                            ? options.find((item) => item.key === defaultKey)?.label ?? defaultKey
                            : t("settings.agents.noDefault"),
                      })}
                    </option>
                    {options.map((item) => (
                      <option key={item.key} value={item.key}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                  {configured && option && !option.model.routing_ready ? (
                    <div className="mt-1 text-[9.5px] text-warning">
                      {t("settings.agents.compatibilityBlockers", {
                        blockers: option.model.blockers.join(", "),
                      })}
                    </div>
                  ) : null}
                </label>
                <label className="block">
                  <span className="mb-1 block text-[10px] text-subtle">{t("settings.agents.reasoning")}</span>
                  <select
                    className="select h-8 text-[11.5px]"
                    value={route?.reasoning ?? "inherit"}
                    disabled={!supportsThinking}
                    onChange={(event) => setReasoning(role, event.target.value as "inherit" | "disabled")}
                  >
                    <option value="inherit">{t("settings.agents.reasoningInherit")}</option>
                    <option value="disabled">{t("settings.agents.reasoningDisabled")}</option>
                  </select>
                  {!supportsThinking ? (
                    <div className="mt-1 text-[9.5px] text-subtle">{t("settings.agents.reasoningUnsupported")}</div>
                  ) : null}
                </label>
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-[10.5px] text-subtle">
            {options.some((item) => item.model.routing_ready) ? (
              <>
                <CheckCircle2 size={12} className="text-success" /> {t("settings.agents.compatibilityOk")}
              </>
            ) : (
              <>
                <TriangleAlert size={12} className="text-warning" /> {t("settings.agents.missingModel")}
              </>
            )}
          </div>
          <Button size="sm" variant="primary" loading={busy} onClick={() => void save()}>
            {t("settings.save")}
          </Button>
        </div>
        <div className="mt-3">
          <Notice tone="info">{t("settings.agents.compatibility")}: {t("settings.scope.newTasks")}.</Notice>
        </div>
      </SettingsSection>
    </div>
  );
}
