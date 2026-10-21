import {
  Check,
  KeyRound,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Search,
  Star,
  TestTube2,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  createProvider,
  createProviderFromTemplate,
  deleteProvider,
  deleteProviderCredential,
  deleteProviderModel,
  discoverProviderModels,
  setProviderCredential,
  testProvider,
  updateProvider,
  updateSettingsSection,
  upsertProviderModel,
} from "../../api/client";
import type {
  CatalogTemplate,
  InferenceStatus,
  ProviderModel,
  ProviderTestResult,
  ProviderTestSummary,
  ProviderView,
  SettingsDocument,
} from "../../api/types";
import type { SettingsNotice } from "../../hooks/useSettings";
import { formatDate, formatNumber } from "../../lib/format";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { OverflowMenu } from "./OverflowMenu";
import { EmptyCard, Field, Segmented, SettingsSection, StatusPill } from "./shared";

type Execute = (
  action: () => Promise<SettingsDocument>,
  successText?: string,
) => Promise<SettingsDocument | null>;

interface ProviderDraft {
  provider_id: string;
  name: string;
  protocol: string;
  base_url: string;
  region: string;
  aws_profile: string;
  timeout_seconds: string;
  max_retries: string;
  max_concurrency: string;
  enabled: boolean;
}

function draftFromProvider(provider: ProviderView): ProviderDraft {
  return {
    provider_id: provider.provider_id,
    name: provider.name,
    protocol: provider.protocol,
    base_url: provider.base_url,
    region: provider.region ?? "",
    aws_profile: provider.aws_profile ?? "",
    timeout_seconds: String(provider.timeout_seconds),
    max_retries: String(provider.max_retries),
    max_concurrency: String(provider.max_concurrency),
    enabled: provider.enabled,
  };
}

function rawModel(model: ProviderModel): Record<string, unknown> {
  return {
    model_id: model.model_id,
    display_name: model.display_name,
    context_window: model.context_window,
    max_output_tokens: model.max_output_tokens,
    supports_tools: model.supports_tools,
    supports_json_mode: model.supports_json_mode,
    supports_temperature: model.supports_temperature,
    supports_thinking: model.supports_thinking,
    price: model.price,
    source: model.source,
    source_note: model.source_note,
    favorite: model.favorite,
  };
}

function uniqueProviderId(base: string, existing: Set<string>): string {
  const normalized = base.toLowerCase().replace(/[^a-z0-9_-]/g, "-").replace(/^-+/, "");
  const seed = normalized.length >= 2 ? normalized : "provider";
  if (!existing.has(seed)) return seed;
  let index = 2;
  while (existing.has(`${seed}-${index}`)) index += 1;
  return `${seed}-${index}`;
}

function InferencePill({ status }: { status: InferenceStatus }) {
  const { t } = useTranslation();
  const tone =
    status.state === "verified"
      ? "success"
      : status.state === "failed"
        ? "danger"
        : status.state === "stale"
          ? "warning"
          : "neutral";
  return <StatusPill label={t(`settings.models.inferenceState.${status.state}`)} tone={tone} />;
}

function BlockerList({ blockers }: { blockers: string[] }) {
  const { t } = useTranslation();
  if (!blockers.length) {
    return <span className="text-success">{t("settings.models.noRoutingBlockers")}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {blockers.map((blocker) => (
        <span
          key={blocker}
          title={blocker}
          className="rounded border border-warning/25 bg-warning/10 px-1.5 py-0.5 text-[9.5px] leading-tight text-warning"
        >
          {t(`settings.models.blockerCodes.${blocker}`, {
            defaultValue: blocker.replaceAll("_", " "),
          })}
        </span>
      ))}
    </div>
  );
}

function TestStateLine({ label, test }: { label: string; test: ProviderTestSummary }) {
  const { t } = useTranslation();
  const tone =
    test.state === "ok"
      ? "success"
      : test.state === "failed"
        ? "danger"
        : test.state === "stale"
          ? "warning"
          : "neutral";
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-muted">{label}</span>
      <StatusPill label={t(`settings.models.testState.${test.state}`)} tone={tone} />
      {test.checked_at ? <span>{formatDate(test.checked_at)}</span> : null}
      {test.state === "failed" && test.detail ? (
        <span className="min-w-0 break-words text-danger">{test.detail}</span>
      ) : null}
    </div>
  );
}

export function ProviderSettings({
  document,
  catalog,
  apiKey,
  busy,
  execute,
  refresh,
  notify,
}: {
  document: SettingsDocument;
  catalog: CatalogTemplate[];
  apiKey: string;
  busy: boolean;
  execute: Execute;
  refresh: () => Promise<void>;
  notify: (notice: SettingsNotice) => void;
}) {
  const { t } = useTranslation();
  const version = document.version;
  const [dialog, setDialog] = useState<
    | { kind: "catalog" }
    | { kind: "create"; template: CatalogTemplate | null }
    | { kind: "edit"; provider: ProviderView }
    | null
  >(null);
  const [draft, setDraft] = useState<ProviderDraft>({
    provider_id: "",
    name: "",
    protocol: "openai_compatible",
    base_url: "",
    region: "",
    aws_profile: "",
    timeout_seconds: "60",
    max_retries: "2",
    max_concurrency: "2",
    enabled: true,
  });
  const [apiKeyEnv, setApiKeyEnv] = useState("");
  const [credentialFor, setCredentialFor] = useState<ProviderView | null>(null);
  const [modelFor, setModelFor] = useState<{ provider: ProviderView; model?: ProviderModel } | null>(null);
  const [testFor, setTestFor] = useState<ProviderView | null>(null);
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [discovery, setDiscovery] = useState<{
    provider: ProviderView;
    result: ProviderTestResult;
    selected: string[];
  } | null>(null);
  const [inferenceConfirm, setInferenceConfirm] = useState<{
    provider: ProviderView;
    modelId: string;
  } | null>(null);
  const [query, setQuery] = useState("");
  const [favoritesOnly, setFavoritesOnly] = useState(false);

  const defaultKey =
    document.agents.default_provider_id && document.agents.default_model_id
      ? `${document.agents.default_provider_id}/${document.agents.default_model_id}`
      : "";

  const filteredProviders = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return document.providers
      .map((provider) => ({
        provider,
        models: provider.models.filter((model) => {
          if (favoritesOnly && !model.favorite) return false;
          if (!needle) return true;
          return `${model.model_id} ${model.display_name}`.toLowerCase().includes(needle);
        }),
      }))
      .filter(({ provider, models }) => {
        if (!needle && !favoritesOnly) return true;
        return models.length > 0 || provider.name.toLowerCase().includes(needle);
      });
  }, [document.providers, favoritesOnly, query]);

  const openCreate = (template: CatalogTemplate | null) => {
    const customTemplate = template?.catalog_id === "custom" ? null : template;
    const existing = new Set(document.providers.map((item) => item.provider_id));
    setApiKeyEnv(customTemplate?.api_key_env ?? "");
    setDraft({
      provider_id: uniqueProviderId(customTemplate?.catalog_id ?? "custom", existing),
      name: customTemplate?.name ?? t("settings.models.customProvider"),
      protocol: customTemplate?.protocol ?? "openai_compatible",
      base_url: customTemplate?.base_url ?? "",
      region: customTemplate?.catalog_id === "bedrock" ? "us-east-1" : "",
      aws_profile: "",
      timeout_seconds: "60",
      max_retries: "2",
      max_concurrency: "2",
      enabled: true,
    });
    setDialog({ kind: "create", template: customTemplate });
  };

  const openEdit = (provider: ProviderView) => {
    setDraft(draftFromProvider(provider));
    setDialog({ kind: "edit", provider });
  };

  const submitProvider = async () => {
    if (!dialog || dialog.kind === "catalog") return;
    if (dialog.kind === "create") {
      const template = dialog.template;
      if (template) {
        const created = await execute(
          () =>
            createProviderFromTemplate(
              apiKey,
              {
                catalog_id: template.catalog_id,
                provider_id: draft.provider_id,
                name: draft.name || undefined,
                base_url: draft.base_url || undefined,
                region: draft.region || undefined,
                api_key_env: apiKeyEnv || undefined,
              },
              version,
            ),
          t("settings.models.created"),
        );
        setDialog(null);
        if (created) {
          const provider = created.providers.find((item) => item.provider_id === draft.provider_id);
          if (provider) setCredentialFor(provider);
        }
        return;
      }
      const payload: Record<string, unknown> = {
        provider_id: draft.provider_id,
        name: draft.name,
        protocol: draft.protocol,
        base_url: draft.base_url,
        enabled: true,
        custom: true,
        timeout_seconds: Number(draft.timeout_seconds),
        max_retries: Number(draft.max_retries),
        max_concurrency: Number(draft.max_concurrency),
        region: draft.protocol === "bedrock_converse" ? draft.region : null,
        aws_profile: draft.aws_profile || null,
        models: [],
        credential: apiKeyEnv ? { kind: "env", name: apiKeyEnv } : { kind: "none" },
      };
      const created = await execute(
        () => createProvider(apiKey, payload, version),
        t("settings.models.created"),
      );
      setDialog(null);
      if (created) {
        const provider = created.providers.find((item) => item.provider_id === draft.provider_id);
        if (provider) {
          if (provider.protocol === "bedrock_converse") {
            setModelFor({ provider });
          } else {
            setCredentialFor(provider);
          }
        }
      }
      return;
    }
    const provider = dialog.provider;
    const editable = provider.editable as Record<string, unknown>;
    const payload: Record<string, unknown> = {
      ...editable,
      provider_id: provider.provider_id,
      name: draft.name,
      base_url: draft.base_url,
      enabled: draft.enabled,
      timeout_seconds: Number(draft.timeout_seconds),
      max_retries: Number(draft.max_retries),
      max_concurrency: Number(draft.max_concurrency),
      region: draft.region || null,
      aws_profile: draft.aws_profile || null,
    };
    const updated = await execute(
      () => updateProvider(apiKey, provider.provider_id, payload, version),
      t("settings.models.updated"),
    );
    if (updated) setDialog(null);
  };

  const setDefaultModel = async (provider: ProviderView, model: ProviderModel) => {
    await execute(
      () =>
        updateSettingsSection(
          apiKey,
          "agents",
          {
            ...document.agents,
            default_provider_id: provider.provider_id,
            default_model_id: model.model_id,
          },
          version,
        ),
      t("settings.saved", { version: version + 1 }),
    );
  };

  const toggleFavorite = async (provider: ProviderView, model: ProviderModel) => {
    const payload = { ...rawModel(model), favorite: !model.favorite };
    await execute(
      () =>
        upsertProviderModel(
          apiKey,
          provider.provider_id,
          payload,
          version,
          model.model_id,
        ),
    );
  };

  const removeProvider = async (provider: ProviderView) => {
    if (!window.confirm(t("settings.models.deleteConfirm", { name: provider.name }))) return;
    await execute(
      () => deleteProvider(apiKey, provider.provider_id, version),
      t("settings.models.deleted"),
    );
  };

  const removeModel = async (provider: ProviderView, model: ProviderModel) => {
    if (!window.confirm(t("settings.models.deleteModelConfirm", { id: model.model_id }))) return;
    await execute(
      () => deleteProviderModel(apiKey, provider.provider_id, model.model_id, version),
    );
  };

  const runTest = async (
    provider: ProviderView,
    kind: "config" | "models" | "inference",
    modelId?: string,
  ) => {
    setTestBusy(true);
    setTestResult(null);
    try {
      const result = await testProvider(apiKey, provider.provider_id, {
        kind,
        model_id: modelId,
        confirm_cost: kind === "inference",
      });
      setTestResult(result);
      await refresh();
      if (kind === "models") {
        setDiscovery({ provider, result, selected: result.models ?? [] });
      }
    } catch (caught) {
      notify({
        tone: "danger",
        text: caught instanceof Error ? caught.message : t("settings.models.configInvalid"),
      });
    } finally {
      setTestBusy(false);
    }
  };

  const runDiscovery = async (provider: ProviderView) => {
    setTestBusy(true);
    try {
      const result = await discoverProviderModels(apiKey, provider.provider_id);
      setTestResult(result);
      await refresh();
      if (result.ok) {
        setDiscovery({ provider, result, selected: result.models ?? [] });
      } else {
        notify({ tone: "warning", text: t("settings.models.discoverNone") });
      }
    } catch (caught) {
      notify({
        tone: "danger",
        text: caught instanceof Error ? caught.message : t("settings.models.configInvalid"),
      });
    } finally {
      setTestBusy(false);
    }
  };

  const addDiscovered = async () => {
    if (!discovery) return;
    let current: SettingsDocument | null = document;
    const ids = new Set(discovery.provider.models.map((item) => item.model_id));
    for (const modelId of discovery.selected) {
      if (ids.has(modelId)) continue;
      const next = await execute(
        () =>
          upsertProviderModel(
            apiKey,
            discovery.provider.provider_id,
            {
              model_id: modelId,
              display_name: modelId,
              context_window: null,
              max_output_tokens: null,
              supports_tools: true,
              supports_json_mode: true,
              supports_temperature: true,
              supports_thinking: null,
              price: { currency: document.execution.budget_currency },
              source: "discovered",
              source_note: null,
              favorite: false,
            },
            current?.version ?? version,
          ),
      );
      if (!next) return;
      current = next;
    }
    notify({ tone: "success", text: t("settings.saved", { version: current?.version ?? version }) });
    setDiscovery(null);
  };

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.models.title")}
        description={t("settings.models.intro")}
        scope="newTasks"
        actions={
          <>
            <Button size="sm" variant="ghost" onClick={() => openCreate(null)} icon={<Plus size={13} />}>
              {t("settings.models.addCustom")}
            </Button>
            <Button size="sm" variant="primary" onClick={() => setDialog({ kind: "catalog" })} icon={<Plus size={13} />}>
              {t("settings.models.addProvider")}
            </Button>
          </>
        }
      >
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle" />
            <input
              className="input h-9 pl-8"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("common.search")}
              aria-label={t("settings.models.filterProvider")}
            />
          </div>
          <button
            type="button"
            className="btn h-9 text-[11.5px]"
            data-active={favoritesOnly}
            onClick={() => setFavoritesOnly((value) => !value)}
          >
            <Star size={13} /> {t("settings.models.favorites")}
          </button>
          <span className="text-[10.5px] text-subtle">
            {t("settings.models.providerCount", { count: document.providers.length })}
          </span>
        </div>

        {!document.providers.length ? (
          <EmptyCard>
            <div className="mb-1 font-medium text-muted">{t("settings.models.noProviders")}</div>
            {t("settings.models.noProvidersDescription")}
          </EmptyCard>
        ) : null}
        {document.providers.length > 0 && filteredProviders.length === 0 ? (
          <EmptyCard>
            <div className="mb-1 font-medium text-muted">{t("settings.models.noMatches")}</div>
            {t("settings.models.noMatchesDescription")}
          </EmptyCard>
        ) : null}

        <div className="space-y-3">
          {filteredProviders.map(({ provider, models }) => {
            const eligible = provider.models.filter(
              (model) => model.verification.routing.eligible,
            ).length;
            const verified = provider.models.filter(
              (model) => model.verification.inference.state === "verified",
            ).length;
            const configured = provider.models.filter(
              (model) => model.verification.config_complete,
            ).length;
            const total = provider.models.length;
            const credentialReady =
              provider.credential.available || provider.protocol === "bedrock_converse";
            return (
              <div key={provider.provider_id} className="rounded-xl border bg-surface">
                <div className="flex flex-wrap items-start gap-3 border-b px-4 py-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[13px] font-semibold">{provider.name}</span>
                      <span className="font-mono text-[10px] text-subtle">{provider.provider_id}</span>
                      <span className="rounded border bg-canvas px-1.5 py-0.5 font-mono text-[9.5px] text-muted">
                        {provider.protocol}
                      </span>
                      {provider.catalog_id ? (
                        <span className="rounded border bg-canvas px-1.5 py-0.5 text-[9.5px] text-subtle">
                          {provider.catalog_id}
                        </span>
                      ) : null}
                      {!provider.enabled ? (
                        <StatusPill label={t("settings.models.disabledBadge")} tone="neutral" />
                      ) : null}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-subtle">
                      <span className="max-w-full truncate font-mono" title={provider.base_url || "AWS SDK"}>
                        {provider.base_url || "AWS SDK"}
                      </span>
                      <span>·</span>
                      <span title={provider.credential.reason}>
                        {provider.credential.kind === "none"
                          ? t("settings.models.credentialMissing")
                          : provider.credential.kind === "env"
                            ? t("settings.models.credentialEnv", {
                                name: provider.credential.env_name ?? "—",
                              })
                            : provider.credential.kind === "vault"
                              ? t("settings.models.credentialVault")
                              : t("settings.models.credentialAws")}
                      </span>
                      <StatusPill
                        label={
                          credentialReady
                            ? t("settings.models.credentialAvailable")
                            : t("settings.models.credentialUnavailable")
                        }
                        tone={credentialReady ? "success" : "warning"}
                      />
                      <span>{t("settings.models.modelCount", { count: total })}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <StatusPill
                        label={t("settings.models.routingSummary", { eligible, total })}
                        tone={total > 0 && eligible === total ? "success" : "warning"}
                      />
                      <StatusPill
                        label={t("settings.models.inferenceSummary", { verified, total })}
                        tone={verified > 0 ? "success" : "neutral"}
                      />
                      <span className="text-[10px] text-subtle">
                        {t("settings.models.configSummary", { configured, total })}
                      </span>
                    </div>
                    <details className="mt-2 text-[10.5px] text-subtle">
                      <summary className="inline-flex cursor-pointer items-center gap-1 hover:text-ink">
                        {t("settings.models.statusDetails")}
                      </summary>
                      <div className="mt-1.5 grid gap-1.5 rounded-lg border bg-canvas/40 p-2.5 sm:grid-cols-2">
                        <TestStateLine
                          label={t("settings.models.configCheck")}
                          test={provider.verification.config_test}
                        />
                        <TestStateLine
                          label={t("settings.models.modelDiscovery")}
                          test={provider.verification.models_test}
                        />
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-muted">{t("settings.models.credentialState")}</span>
                          <StatusPill
                            label={
                              credentialReady
                                ? t("settings.models.credentialAvailable")
                                : t("settings.models.credentialUnavailable")
                            }
                            tone={credentialReady ? "success" : "warning"}
                          />
                          <span className="min-w-0 break-words">{provider.credential.reason}</span>
                        </div>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-muted">{t("settings.models.protocolState")}</span>
                          <StatusPill
                            label={
                              provider.protocol_supported
                                ? t("settings.models.protocolAvailable")
                                : t("settings.models.protocolUnavailable")
                            }
                            tone={provider.protocol_supported ? "success" : "warning"}
                          />
                          <span className="min-w-0 break-words">{provider.protocol_reason}</span>
                        </div>
                        <p className="leading-relaxed sm:col-span-2">
                          {t("settings.models.routingIsNotVerification")}
                        </p>
                      </div>
                    </details>
                  </div>
                  <div className="flex flex-wrap items-center gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setTestFor(provider);
                        setTestResult(null);
                        void runTest(provider, "config");
                      }}
                      icon={<PlugZap size={12} />}
                    >
                      {t("settings.models.testConfig")}
                    </Button>
                    <OverflowMenu
                      label={t("common.moreActions")}
                      items={[
                        {
                          key: "edit",
                          label: t("settings.models.edit"),
                          icon: <Pencil size={12} />,
                          onSelect: () => openEdit(provider),
                        },
                        ...(provider.protocol !== "bedrock_converse"
                          ? [
                              {
                                key: "credential",
                                label: t("settings.models.credential"),
                                icon: <KeyRound size={12} />,
                                onSelect: () => setCredentialFor(provider),
                              },
                            ]
                          : []),
                        {
                          key: "discover",
                          label: t("settings.models.discover"),
                          icon: <RefreshCw size={12} />,
                          onSelect: () => void runDiscovery(provider),
                          disabled: testBusy,
                        },
                        {
                          key: "add-model",
                          label: t("settings.models.addModel"),
                          icon: <Plus size={12} />,
                          onSelect: () => setModelFor({ provider }),
                        },
                        {
                          key: "delete",
                          label: t("settings.models.delete"),
                          icon: <Trash2 size={12} />,
                          onSelect: () => void removeProvider(provider),
                          danger: true,
                        },
                      ]}
                    />
                  </div>
                </div>

                <div className="px-4 py-3">
                  {!models.length ? (
                    <div className="text-[11.5px] text-subtle">{t("settings.models.modelsEmpty")}</div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[700px] table-fixed text-[11px]">
                        <thead>
                          <tr className="border-b text-left text-[9.5px] uppercase tracking-[0.08em] text-subtle">
                            <th className="w-[29%] py-1.5 pr-2 font-medium">
                              {t("settings.models.modelId")}
                            </th>
                            <th className="w-[12%] py-1.5 pr-2 font-medium">
                              {t("settings.models.limits")}
                            </th>
                            <th className="w-[8%] py-1.5 pr-2 font-medium">
                              {t("settings.models.supportsTools")}
                            </th>
                            <th className="w-[17%] py-1.5 pr-2 font-medium">
                              {t("settings.models.pricing")}
                            </th>
                            <th className="w-[28%] py-1.5 pr-2 font-medium">
                              {t("settings.models.status")}
                            </th>
                            <th className="w-[6%] py-1.5 font-medium">{t("common.actions")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {models.map((model) => {
                            const isDefault =
                              defaultKey === `${provider.provider_id}/${model.model_id}`;
                            const verification = model.verification;
                            return (
                              <tr key={model.model_id} className="border-b last:border-0 align-top">
                                <td className="py-2 pr-2">
                                  <div className="flex min-w-0 items-start gap-1.5">
                                    <button
                                      type="button"
                                      className="mt-0.5 text-subtle hover:text-warning"
                                      onClick={() => void toggleFavorite(provider, model)}
                                      aria-label={t("settings.models.favorite")}
                                    >
                                      <Star
                                        size={12}
                                        className={model.favorite ? "fill-current text-warning" : ""}
                                      />
                                    </button>
                                    <div className="min-w-0">
                                      <div
                                        className="break-all font-mono leading-snug"
                                        title={model.model_id}
                                      >
                                        {model.model_id}
                                      </div>
                                      <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[9.5px] text-subtle">
                                        <span
                                          className="truncate"
                                          title={model.display_name || model.model_id}
                                        >
                                          {model.display_name || model.model_id}
                                        </span>
                                        {isDefault ? (
                                          <span className="rounded border bg-canvas px-1 py-0.5 text-ink">
                                            {t("settings.models.defaultBadge")}
                                          </span>
                                        ) : null}
                                        <span
                                          className="rounded border bg-canvas px-1 py-0.5"
                                          title={model.source_note ?? undefined}
                                        >
                                          {t(`settings.models.modelSource.${model.source}`)}
                                        </span>
                                      </div>
                                    </div>
                                  </div>
                                </td>
                                <td className="py-2 pr-2 font-mono text-[10px]">
                                  <div className="text-subtle">
                                    {t("settings.models.contextShort")}{" "}
                                    {formatNumber(model.context_window)}
                                  </div>
                                  <div className="text-subtle">
                                    {t("settings.models.outputShort")}{" "}
                                    {formatNumber(model.max_output_tokens)}
                                  </div>
                                </td>
                                <td className="py-2 pr-2">
                                  <div>{model.supports_tools ? t("common.yes") : t("common.no")}</div>
                                  <div className="text-[9px] text-subtle">
                                    {t("settings.models.declared")}
                                  </div>
                                </td>
                                <td className="py-2 pr-2">
                                  {model.price.input_per_mtok !== null &&
                                  model.price.output_per_mtok !== null ? (
                                    <div className="break-words font-mono text-[10px]">
                                      {model.price.currency} {model.price.input_per_mtok}/
                                      {model.price.output_per_mtok}
                                    </div>
                                  ) : (
                                    <div className="text-warning">
                                      {t("settings.models.priceMissing")}
                                    </div>
                                  )}
                                  <div className="mt-0.5 text-[9px] leading-tight text-subtle">
                                    {t(`settings.models.priceSource.${model.source}`, {
                                      defaultValue: t("settings.models.priceSource.manual"),
                                    })}
                                  </div>
                                </td>
                                <td className="py-2 pr-2">
                                  <div className="flex flex-wrap gap-1">
                                    <StatusPill
                                      label={
                                        verification.routing.eligible
                                          ? t("settings.models.routingConditions")
                                          : t("settings.models.routingBlocked")
                                      }
                                      tone={verification.routing.eligible ? "success" : "warning"}
                                    />
                                    <InferencePill status={verification.inference} />
                                  </div>
                                  <div className="mt-1">
                                    <BlockerList blockers={verification.routing.blockers} />
                                  </div>
                                  {verification.inference.checked_at ? (
                                    <div className="mt-1 text-[9.5px] text-subtle">
                                      {t("settings.models.lastChecked", {
                                        time: formatDate(verification.inference.checked_at),
                                      })}
                                    </div>
                                  ) : null}
                                  {verification.inference.detail ? (
                                    <div
                                      className="mt-1 break-words text-[9.5px] leading-tight text-danger"
                                      title={verification.inference.detail}
                                    >
                                      {verification.inference.detail}
                                    </div>
                                  ) : null}
                                  {verification.tool_calling === "declared_not_verified" ? (
                                    <div className="mt-1 text-[9px] text-subtle">
                                      {t("settings.models.toolsDeclaredNotVerified")}
                                    </div>
                                  ) : null}
                                </td>
                                <td className="py-2">
                                  <OverflowMenu
                                    label={t("settings.models.modelActions", {
                                      id: model.model_id,
                                    })}
                                    items={[
                                      {
                                        key: "default",
                                        label: t("settings.models.setDefault"),
                                        icon: <Star size={12} />,
                                        onSelect: () => void setDefaultModel(provider, model),
                                        disabled: isDefault,
                                      },
                                      {
                                        key: "edit",
                                        label: t("common.edit"),
                                        icon: <Pencil size={12} />,
                                        onSelect: () => setModelFor({ provider, model }),
                                      },
                                      {
                                        key: "inference",
                                        label: t("settings.models.testInference"),
                                        icon: <TestTube2 size={12} />,
                                        onSelect: () =>
                                          setInferenceConfirm({
                                            provider,
                                            modelId: model.model_id,
                                          }),
                                      },
                                      {
                                        key: "delete",
                                        label: t("settings.models.deleteModel"),
                                        icon: <Trash2 size={12} />,
                                        onSelect: () => void removeModel(provider, model),
                                        danger: true,
                                      },
                                    ]}
                                  />
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </SettingsSection>

      <Modal
        open={dialog?.kind === "catalog"}
        title={t("settings.models.catalog")}
        description={t("settings.models.catalogHint")}
        onClose={() => setDialog(null)}
        width="max-w-3xl"
      >
        <div className="grid gap-2 sm:grid-cols-2">
          {catalog.map((template) => (
            <button
              key={template.catalog_id}
              className="flex flex-col items-start gap-1 rounded-xl border bg-surface px-3.5 py-3 text-left transition-colors hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-60"
              disabled={!template.available || !template.protocol_supported}
              onClick={() => openCreate(template)}
            >
              <div className="flex w-full items-center gap-2">
                <span className="text-[13px] font-medium">{template.name}</span>
                <span className="ml-auto font-mono text-[9.5px] text-subtle">{template.protocol}</span>
              </div>
              <div className="text-[10.5px] leading-relaxed text-subtle">{template.notes}</div>
              <div className="mt-0.5 flex flex-wrap gap-1">
                <StatusPill
                  label={template.available ? t("settings.models.catalogAvailable") : t("settings.models.catalogUnavailable")}
                  tone={template.available ? "success" : "warning"}
                />
                <span className="rounded border px-1.5 py-0.5 text-[9px] text-subtle">{template.source}</span>
              </div>
            </button>
          ))}
          <button
            className="flex flex-col items-start gap-1 rounded-xl border border-dashed bg-canvas/40 px-3.5 py-3 text-left transition-colors hover:bg-elevated"
            onClick={() => openCreate(null)}
          >
            <span className="text-[13px] font-medium">{t("settings.models.customProvider")}</span>
            <div className="text-[10.5px] leading-relaxed text-subtle">{t("settings.models.customHint")}</div>
          </button>
        </div>
      </Modal>

      <Modal
        open={dialog?.kind === "create" || dialog?.kind === "edit"}
        title={dialog?.kind === "edit" ? t("settings.models.edit") : t("settings.models.addProvider")}
        description={
          dialog?.kind === "create" && dialog.template
            ? t("settings.models.catalogSource", { source: dialog.template.source })
            : t("settings.models.advancedProvider")
        }
        onClose={() => setDialog(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setDialog(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant="primary" loading={busy} onClick={() => void submitProvider()}>
              {dialog?.kind === "edit" ? t("common.save") : t("settings.models.createProvider")}
            </Button>
          </>
        }
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("settings.models.providerId")} hint={t("settings.models.providerIdHint")}>
            <input
              className="input h-9 font-mono text-[11.5px]"
              value={draft.provider_id}
              disabled={dialog?.kind === "edit"}
              onChange={(event) => setDraft({ ...draft, provider_id: event.target.value })}
            />
          </Field>
          <Field label={t("settings.models.providerName")}>
            <input
              className="input h-9 text-[12px]"
              value={draft.name}
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            />
          </Field>
          {dialog?.kind === "edit" ? (
            <Field label={t("common.status")}>
              <label className="flex h-9 items-center gap-2 text-[12px]">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
                />
                {t("common.enabled")}
              </label>
            </Field>
          ) : null}
          <Field label={t("settings.models.protocol")}>
            <select
              className="select h-9 text-[12px]"
              value={draft.protocol}
              disabled={dialog?.kind === "edit" || Boolean(dialog?.kind === "create" && dialog.template)}
              onChange={(event) => setDraft({ ...draft, protocol: event.target.value })}
            >
              <option value="openai_compatible">OpenAI Compatible</option>
              <option value="anthropic_messages">Anthropic Messages</option>
              <option value="bedrock_converse">AWS Bedrock Converse</option>
            </select>
          </Field>
          {draft.protocol === "bedrock_converse" ? (
            <>
              <Field label={t("settings.models.awsRegion")}>
                <input
                  className="input h-9 text-[12px]"
                  value={draft.region}
                  onChange={(event) => setDraft({ ...draft, region: event.target.value })}
                />
              </Field>
              <Field label={t("settings.models.awsProfile")} hint={t("settings.models.awsHint")}>
                <input
                  className="input h-9 text-[12px]"
                  value={draft.aws_profile}
                  onChange={(event) => setDraft({ ...draft, aws_profile: event.target.value })}
                />
              </Field>
            </>
          ) : (
            <>
              <Field label={t("settings.models.baseUrl")} hint={t("settings.models.baseUrlHint")} wide>
                <input
                  className="input h-9 font-mono text-[11.5px]"
                  value={draft.base_url}
                  onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
                  placeholder="https://api.example.com/v1"
                />
              </Field>
              {dialog?.kind === "create" ? (
                <Field label={t("settings.models.credentialEnvName")} hint={t("settings.models.credentialEnvHint")}>
                  <input
                    className="input h-9 font-mono text-[11.5px]"
                    value={apiKeyEnv}
                    onChange={(event) => setApiKeyEnv(event.target.value)}
                    placeholder="MY_PROVIDER_API_KEY"
                  />
                </Field>
              ) : null}
            </>
          )}
          <details className="sm:col-span-2">
            <summary className="cursor-pointer text-[11.5px] text-muted">
              {t("settings.models.advancedProvider")}
            </summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <Field label={t("settings.models.timeout")}>
                <input
                  className="input h-9 text-[12px]"
                  type="number"
                  min={5}
                  max={600}
                  value={draft.timeout_seconds}
                  onChange={(event) => setDraft({ ...draft, timeout_seconds: event.target.value })}
                />
              </Field>
              <Field label={t("settings.models.retries")}>
                <input
                  className="input h-9 text-[12px]"
                  type="number"
                  min={0}
                  max={5}
                  value={draft.max_retries}
                  onChange={(event) => setDraft({ ...draft, max_retries: event.target.value })}
                />
              </Field>
              <Field label={t("settings.models.concurrency")}>
                <input
                  className="input h-9 text-[12px]"
                  type="number"
                  min={1}
                  max={16}
                  value={draft.max_concurrency}
                  onChange={(event) => setDraft({ ...draft, max_concurrency: event.target.value })}
                />
              </Field>
            </div>
          </details>
        </div>
      </Modal>

      <CredentialDialog
        provider={credentialFor}
        apiKey={apiKey}
        version={document.version}
        busy={busy}
        execute={execute}
        onClose={() => setCredentialFor(null)}
      />

      <ModelDialog
        editing={modelFor}
        apiKey={apiKey}
        version={document.version}
        busy={busy}
        execute={execute}
        onClose={() => setModelFor(null)}
      />

      <Modal
        open={Boolean(discovery)}
        title={t("settings.models.discover")}
        description={t("settings.models.discoverHint")}
        onClose={() => setDiscovery(null)}
        width="max-w-2xl"
        footer={
          <>
            <Button variant="ghost" onClick={() => setDiscovery(null)}>
              {t("common.close")}
            </Button>
            <Button
              variant="primary"
              loading={busy}
              disabled={!discovery?.selected.length}
              onClick={() => void addDiscovered()}
            >
              {t("settings.models.addDiscovered")}
            </Button>
          </>
        }
      >
        {discovery ? (
          <div className="space-y-3">
            <div className="text-[11.5px] text-muted">
              {t("settings.models.discovered", { count: discovery.result.models?.length ?? 0 })}
            </div>
            {!discovery.result.models?.length ? (
              <Notice tone="warning">{t("settings.models.discoverNone")}</Notice>
            ) : (
              <>
                <div className="text-[11px] text-muted">{t("settings.models.selectModels")}</div>
                <div className="max-h-[42vh] space-y-1 overflow-y-auto scroll-thin rounded-lg border bg-canvas/40 p-2">
                  {discovery.result.models.map((modelId) => (
                    <label key={modelId} className="flex items-center gap-2 rounded px-1.5 py-1 font-mono text-[11px] hover:bg-elevated">
                      <input
                        type="checkbox"
                        checked={discovery.selected.includes(modelId)}
                        onChange={(event) =>
                          setDiscovery({
                            ...discovery,
                            selected: event.target.checked
                              ? [...discovery.selected, modelId]
                              : discovery.selected.filter((item) => item !== modelId),
                          })
                        }
                      />
                      {modelId}
                    </label>
                  ))}
                </div>
              </>
            )}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(testFor)}
        title={t("settings.models.testResult")}
        description={testFor ? `${testFor.name} · ${testFor.protocol}` : undefined}
        onClose={() => {
          setTestFor(null);
          setTestResult(null);
        }}
        width="max-w-2xl"
      >
        {testBusy ? <div className="skeleton h-24 w-full" /> : null}
        {!testBusy && testResult ? <TestResultView result={testResult} /> : null}
      </Modal>

      <Modal
        open={Boolean(inferenceConfirm)}
        title={t("settings.models.testInference")}
        description={t("settings.models.inferenceConfirm")}
        onClose={() => setInferenceConfirm(null)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setInferenceConfirm(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              loading={testBusy}
              onClick={() => {
                if (!inferenceConfirm) return;
                const target = inferenceConfirm;
                setInferenceConfirm(null);
                setTestFor(target.provider);
                void runTest(target.provider, "inference", target.modelId);
              }}
              icon={<TestTube2 size={13} />}
            >
              {t("settings.models.testInference")}
            </Button>
          </>
        }
      >
        <Notice tone="warning">{t("settings.models.inferenceConfirm")}</Notice>
      </Modal>
    </div>
  );
}

function TestResultView({ result }: { result: ProviderTestResult }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
        <StatusPill
          label={result.ok ? t("settings.models.configOk") : t("settings.models.configInvalid")}
          tone={result.ok ? "success" : "danger"}
        />
        {result.kind === "inference" ? (
          <StatusPill
            label={result.authenticated ? t("settings.models.authOk") : t("settings.models.authFailed")}
            tone={result.authenticated ? "success" : "danger"}
          />
        ) : null}
        {typeof result.latency_ms === "number" ? (
          <span className="text-subtle">{t("settings.models.latency", { ms: result.latency_ms })}</span>
        ) : null}
        {result.tool_calling ? (
          <span className="text-subtle">
            {result.tool_calling === "not_supported"
              ? t("settings.models.toolNotSupported")
              : t("settings.models.toolDeclared")}
          </span>
        ) : null}
      </div>
      <div className="space-y-1 rounded-lg border bg-canvas/40 p-2.5">
        {result.checks.map((check) => (
          <div key={`${check.name}-${check.detail}`} className="flex items-start gap-2 text-[10.5px]">
            <span className={check.ok ? "text-success" : "text-danger"}>
              {check.ok ? <Check size={11} /> : <X size={11} />}
            </span>
            <span className="font-mono">{check.name}</span>
            <span className="min-w-0 flex-1 text-subtle">{check.detail}</span>
          </div>
        ))}
      </div>
      {result.kind === "inference" && result.usage ? (
        <div className="text-[10.5px] text-subtle">
          {result.usage.input_tokens} in / {result.usage.output_tokens} out · {result.cost_known ? `${result.cost ?? 0} ${result.currency ?? ""}` : t("stages.costUnknown")}
        </div>
      ) : null}
      {result.models?.length ? (
        <div className="max-h-44 overflow-y-auto scroll-thin rounded border bg-canvas/40 p-2 font-mono text-[10px]">
          {result.models.map((item) => (
            <div key={item}>{item}</div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CredentialDialog({
  provider,
  apiKey,
  version,
  busy,
  execute,
  onClose,
}: {
  provider: ProviderView | null;
  apiKey: string;
  version: number;
  busy: boolean;
  execute: Execute;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"value" | "env">("value");
  const [value, setValue] = useState("");
  const [envName, setEnvName] = useState("");

  useEffect(() => {
    if (!provider) return;
    setMode(provider.credential.kind === "env" ? "env" : "value");
    setValue("");
    setEnvName(provider.credential.env_name ?? "");
  }, [provider]);

  if (!provider) return null;

  const submit = async () => {
    const next = await execute(
      () =>
        setProviderCredential(
          apiKey,
          provider.provider_id,
          mode === "value" ? { mode: "value", value } : { mode: "env", env_name: envName },
          version,
        ),
      t("settings.models.credentialSaved"),
    );
    if (next) onClose();
  };

  const revoke = async () => {
    if (!window.confirm(t("settings.models.credentialRevokeConfirm", { name: provider.name }))) return;
    const next = await execute(
      () => deleteProviderCredential(apiKey, provider.provider_id, version),
      t("settings.models.credentialRevoked"),
    );
    if (next) onClose();
  };

  return (
    <Modal
      open
      title={provider.credential.configured ? t("settings.models.replaceCredential") : t("settings.models.setCredential")}
      description={t("settings.models.credentialStoredHint")}
      onClose={onClose}
      footer={
        <>
          {provider.credential.configured ? (
            <Button variant="danger" onClick={() => void revoke()} loading={busy} icon={<Trash2 size={13} />}>
              {t("settings.models.revokeCredential")}
            </Button>
          ) : null}
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={mode === "value" ? value.trim().length < 4 : envName.trim().length < 3}
            onClick={() => void submit()}
            icon={<KeyRound size={13} />}
          >
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Segmented
          value={mode}
          onChange={(next) => setMode(next)}
          options={[
            { value: "value", label: t("settings.models.credentialValue") },
            { value: "env", label: t("settings.models.credentialEnvName") },
          ]}
        />
        {mode === "value" ? (
          <Field label={t("settings.models.credentialValue")}>
            <input
              className="input h-9 font-mono text-[11.5px]"
              type="password"
              autoComplete="off"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              placeholder={t("settings.models.credentialValuePlaceholder")}
            />
          </Field>
        ) : (
          <Field label={t("settings.models.credentialEnvName")} hint={t("settings.models.credentialEnvHint")}>
            <input
              className="input h-9 font-mono text-[11.5px]"
              value={envName}
              onChange={(event) => setEnvName(event.target.value)}
              placeholder="MY_PROVIDER_API_KEY"
            />
          </Field>
        )}
        <div className="text-[10.5px] text-subtle">
          {t("settings.secureStorage", {
            state: provider.credential.available
              ? t("settings.secureStorageAvailable")
              : t("settings.secureStorageUnavailable"),
          })}
        </div>
        {provider.credential.kind !== "none" ? (
          <Notice tone="info">
            {provider.credential.kind === "env"
              ? t("settings.models.credentialEnv", { name: provider.credential.env_name ?? "" })
              : t("settings.models.credentialVault")}
            {" · "}
            {t("settings.models.credentialConfigured", {
              source: provider.credential.reason,
              date: provider.credential.updated_at ?? t("common.notRecorded"),
            })}
          </Notice>
        ) : null}
        {provider.credential.kind === "none" && provider.protocol !== "bedrock_converse" ? (
          <Notice tone="warning">{t("settings.models.credentialMissing")}</Notice>
        ) : null}
      </div>
    </Modal>
  );
}

function ModelDialog({
  editing,
  apiKey,
  version,
  busy,
  execute,
  onClose,
}: {
  editing: { provider: ProviderView; model?: ProviderModel } | null;
  apiKey: string;
  version: number;
  busy: boolean;
  execute: Execute;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const provider = editing?.provider ?? null;
  const editingModel = editing?.model;
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [originalId, setOriginalId] = useState<string | null>(null);

  useEffect(() => {
    if (!provider) return;
    if (editingModel) {
      setDraft(rawModel(editingModel));
      setOriginalId(editingModel.model_id);
      return;
    }
    setDraft({
      model_id: "",
      display_name: "",
      context_window: null,
      max_output_tokens: null,
      supports_tools: true,
      supports_json_mode: true,
      supports_temperature: true,
      supports_thinking: null,
      price: { currency: "CNY", input_per_mtok: null, output_per_mtok: null, cached_input_per_mtok: null },
      source: "manual",
      source_note: null,
      favorite: false,
    });
    setOriginalId(null);
  }, [provider, editingModel]);

  if (!provider || !draft) return null;

  const price = (draft.price ?? {}) as Record<string, unknown>;
  const submit = async () => {
    const next = await execute(
      () =>
        upsertProviderModel(
          apiKey,
          provider.provider_id,
          draft,
          version,
          originalId,
        ),
    );
    if (next) onClose();
  };

  return (
    <Modal
      open
      title={t("settings.models.modelsTitle", { name: provider.name })}
      description={t("settings.models.modelPriceHint")}
      onClose={onClose}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!String(draft.model_id ?? "").trim()}
            onClick={() => void submit()}
          >
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label={t("settings.models.modelId")}>
          <input
            className="input h-9 font-mono text-[11.5px]"
            value={String(draft.model_id ?? "")}
            onChange={(event) => setDraft({ ...draft, model_id: event.target.value })}
          />
        </Field>
        <Field label={t("settings.models.displayName")}>
          <input
            className="input h-9 text-[12px]"
            value={String(draft.display_name ?? "")}
            onChange={(event) => setDraft({ ...draft, display_name: event.target.value })}
          />
        </Field>
        <Field label={t("settings.models.contextWindow")}>
          <input
            className="input h-9 text-[12px]"
            type="number"
            value={draft.context_window === null ? "" : String(draft.context_window)}
            onChange={(event) =>
              setDraft({ ...draft, context_window: event.target.value ? Number(event.target.value) : null })
            }
          />
        </Field>
        <Field label={t("settings.models.maxOutput")}>
          <input
            className="input h-9 text-[12px]"
            type="number"
            value={draft.max_output_tokens === null ? "" : String(draft.max_output_tokens)}
            onChange={(event) =>
              setDraft({ ...draft, max_output_tokens: event.target.value ? Number(event.target.value) : null })
            }
          />
        </Field>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          ["supports_tools", t("settings.models.supportsTools")],
          ["supports_json_mode", t("settings.models.supportsJson")],
          ["supports_temperature", t("settings.models.supportsTemperature")],
        ].map(([key, label]) => (
          <label key={key} className="flex items-center gap-2 rounded border bg-canvas/40 px-2 py-1.5 text-[11px]">
            <input
              type="checkbox"
              checked={Boolean(draft[key])}
              onChange={(event) => setDraft({ ...draft, [key]: event.target.checked })}
            />
            {label}
          </label>
        ))}
        <label className="flex items-center gap-2 rounded border bg-canvas/40 px-2 py-1.5 text-[11px]">
          <input
            type="checkbox"
            checked={draft.supports_thinking === true}
            onChange={(event) => setDraft({ ...draft, supports_thinking: event.target.checked ? true : null })}
          />
          {t("settings.models.supportsThinking")}
        </label>
      </div>
      <details className="mt-3" open>
        <summary className="cursor-pointer text-[11.5px] text-muted">{t("settings.models.pricing")}</summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-4">
          <Field label={t("settings.models.currency")}>
            <input
              className="input h-9 text-[12px]"
              value={String(price.currency ?? "CNY")}
              onChange={(event) => setDraft({ ...draft, price: { ...price, currency: event.target.value.toUpperCase() } })}
            />
          </Field>
          <Field label={t("settings.models.inputPrice")}>
            <input
              className="input h-9 text-[12px]"
              type="number"
              step="0.01"
              value={price.input_per_mtok === null ? "" : String(price.input_per_mtok)}
              onChange={(event) =>
                setDraft({ ...draft, price: { ...price, input_per_mtok: event.target.value ? Number(event.target.value) : null } })
              }
            />
          </Field>
          <Field label={t("settings.models.outputPrice")}>
            <input
              className="input h-9 text-[12px]"
              type="number"
              step="0.01"
              value={price.output_per_mtok === null ? "" : String(price.output_per_mtok)}
              onChange={(event) =>
                setDraft({ ...draft, price: { ...price, output_per_mtok: event.target.value ? Number(event.target.value) : null } })
              }
            />
          </Field>
          <Field label={t("settings.models.cachedPrice")}>
            <input
              className="input h-9 text-[12px]"
              type="number"
              step="0.01"
              value={price.cached_input_per_mtok === null ? "" : String(price.cached_input_per_mtok)}
              onChange={(event) =>
                setDraft({ ...draft, price: { ...price, cached_input_per_mtok: event.target.value ? Number(event.target.value) : null } })
              }
            />
          </Field>
        </div>
      </details>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label={t("settings.models.sourceNote")}>
          <input
            className="input h-9 text-[12px]"
            value={String(draft.source_note ?? "")}
            onChange={(event) => setDraft({ ...draft, source_note: event.target.value || null })}
          />
        </Field>
        <label className="flex items-center gap-2 self-end text-[11.5px] text-muted">
          <input
            type="checkbox"
            checked={Boolean(draft.favorite)}
            onChange={(event) => setDraft({ ...draft, favorite: event.target.checked })}
          />
          {t("settings.models.favorite")}
        </label>
      </div>
      {provider.models.length ? (
        <details className="mt-3">
          <summary className="cursor-pointer text-[11.5px] text-muted">
            {t("settings.models.modelsTitle", { name: provider.name })}
          </summary>
          <div className="mt-2 space-y-1">
            {provider.models.map((model) => (
              <button
                key={model.model_id}
                className="flex w-full items-center gap-2 rounded border bg-canvas/40 px-2 py-1.5 text-left text-[11px] hover:bg-elevated"
                onClick={() => {
                  setDraft(rawModel(model));
                  setOriginalId(model.model_id);
                }}
              >
                <span className="font-mono">{model.model_id}</span>
                <span className="ml-auto text-[9.5px] text-subtle">{model.display_name}</span>
              </button>
            ))}
          </div>
        </details>
      ) : null}
    </Modal>
  );
}
