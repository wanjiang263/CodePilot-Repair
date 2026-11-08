import { ArrowRight, ChevronDown, FlaskConical, GitBranch, LockKeyhole, SlidersHorizontal } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import { ApiError, createTask } from "../api/client";
import type { Meta, Task } from "../api/types";
import { Button } from "./ui/Button";
import { Notice } from "./ui/Notice";

const EXAMPLE_ISSUE =
  "add(2, 3) returns 4 instead of 5. The contract says add() must return the exact sum, and the existing test tests/test_calc.py::test_add fails.";

type ExecutionMode = "offline_fake" | "docker" | "live";

function errorKey(code: string | undefined): string | null {
  switch (code) {
    case "live_cost_cap_required":
    case "live_cost_cap_too_high":
      return "composer.costCapRequired";
    case "live_requires_docker":
    case "live_llm_disabled":
      return "composer.liveDisabled";
    case "live_concurrency_limit":
      return "composer.liveWarning";
    default:
      return null;
  }
}

function liveReason(meta: Meta | null, t: TFunction): string {
  const code = meta?.live_llm_reason_code;
  if (code) return t(`composer.liveReason.${code}`);
  return meta?.live_llm_reason ?? "";
}

export function Composer({
  meta,
  apiKey,
  onCreated,
}: {
  meta: Meta | null;
  apiKey: string;
  onCreated: (task: Task) => void;
}) {
  const { t } = useTranslation();
  const [repositoryKind, setRepositoryKind] = useState<"demo" | "https">("demo");
  const [demoName, setDemoName] = useState("");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [issue, setIssue] = useState("");
  const [baseCommit, setBaseCommit] = useState("HEAD");
  const [showBudget, setShowBudget] = useState(false);
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("offline_fake");
  const [modelChoice, setModelChoice] = useState("");
  const [maxIterations, setMaxIterations] = useState("5");
  const [maxTestRuns, setMaxTestRuns] = useState("12");
  const [maxCost, setMaxCost] = useState("");
  const [maxRuntime, setMaxRuntime] = useState("1800");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The demo repository defaults to the first server-side demo repo until the
  // user picks one; derived during render (replaces the old sync effect).
  const effectiveDemoName =
    demoName || (meta?.demo_repositories.length ? meta.demo_repositories[0].name : "");

  const [lastMeta, setLastMeta] = useState(meta);

  // Seed the budget / mode / repository defaults synchronously during render
  // when new metadata arrives (the sanctioned "adjust state" pattern).
  if (lastMeta !== meta) {
    setLastMeta(meta);
    if (meta) {
      setMaxIterations(String(meta.default_budget.max_iterations ?? 5));
      setMaxTestRuns(String(meta.default_budget.max_test_runs ?? 12));
      setMaxRuntime(String(meta.default_budget.max_runtime_seconds ?? 1800));
      if (meta.default_budget.max_cost !== null && meta.default_budget.max_cost !== undefined) {
        setMaxCost(String(meta.default_budget.max_cost));
      }
      const preferred = meta.default_execution_mode;
      if (preferred && meta.execution_modes.includes(preferred)) {
        setExecutionMode(preferred);
      }
      if (meta.workspace_defaults) {
        setRepositoryKind(meta.workspace_defaults.default_repository_kind);
        if (meta.workspace_defaults.default_repository_kind === "https") {
          setRepositoryUrl(meta.workspace_defaults.default_repository);
        }
      }
    }
  }

  const readyModels = useMemo(
    () => (meta?.live_models ?? []).filter((item) => item.ready),
    [meta],
  );
  const allowedHosts = useMemo(() => meta?.allowed_repository_hosts ?? [], [meta]);
  const urlError = useMemo(() => {
    if (repositoryKind !== "https" || !repositoryUrl.trim()) return null;
    try {
      const parsed = new URL(repositoryUrl.trim());
      if (parsed.protocol !== "https:") return t("composer.onlyHttps");
      if (!allowedHosts.includes(parsed.hostname.toLowerCase())) {
        return t("composer.hostNotAllowed", { hosts: allowedHosts.join(", ") || "—" });
      }
      if (parsed.username || parsed.password) return t("composer.noCredentialsInUrl");
    } catch {
      return t("composer.invalidUrl");
    }
    return null;
  }, [allowedHosts, repositoryKind, repositoryUrl, t]);

  const live = executionMode === "live";
  // Fall back to the preferred (or first ready) model during render whenever
  // the current choice is unavailable (replaces the old sync effect).
  const effectiveModelChoice = useMemo(() => {
    if (modelChoice && readyModels.some((item) => `${item.provider_id}/${item.model_id}` === modelChoice)) {
      return modelChoice;
    }
    const preferred = meta?.default_model;
    const preferredKey =
      preferred?.provider_id && preferred.model_id
        ? `${preferred.provider_id}/${preferred.model_id}`
        : null;
    if (preferredKey && readyModels.some((item) => `${item.provider_id}/${item.model_id}` === preferredKey)) {
      return preferredKey;
    }
    return readyModels.length
      ? `${readyModels[0].provider_id}/${readyModels[0].model_id}`
      : modelChoice;
  }, [meta, modelChoice, readyModels]);
  const selectedModel = readyModels.find(
    (item) => `${item.provider_id}/${item.model_id}` === effectiveModelChoice,
  );
  const canSubmit =
    issue.trim().length >= 10 &&
    !submitting &&
    (repositoryKind === "demo" ? Boolean(effectiveDemoName) : Boolean(repositoryUrl.trim()) && !urlError) &&
    (!live || (Boolean(selectedModel) && Number(maxCost) > 0));

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const budget: Record<string, unknown> = {};
      if (maxIterations && Number(maxIterations) !== meta?.default_budget.max_iterations) {
        budget.max_iterations = Number(maxIterations);
      }
      if (maxTestRuns && Number(maxTestRuns) !== meta?.default_budget.max_test_runs) {
        budget.max_test_runs = Number(maxTestRuns);
      }
      if (maxRuntime && Number(maxRuntime) !== meta?.default_budget.max_runtime_seconds) {
        budget.max_runtime_seconds = Number(maxRuntime);
      }
      if (maxCost.trim()) budget.max_cost = Number(maxCost);
      const task = await createTask(apiKey, {
        repository:
          repositoryKind === "demo"
            ? { kind: "demo", value: effectiveDemoName }
            : { kind: "https", value: repositoryUrl.trim() },
        issue: issue.trim(),
        base_commit: baseCommit.trim() || "HEAD",
        execution_mode: executionMode,
        budget: Object.keys(budget).length ? budget : undefined,
        model: live && selectedModel
          ? { provider_id: selectedModel.provider_id, model_id: selectedModel.model_id }
          : undefined,
      });
      onCreated(task);
    } catch (caught) {
      if (caught instanceof ApiError) {
        const key = errorKey(caught.code);
        setError(
          key
            ? t(key, { reason: meta?.live_llm_reason ?? caught.message })
            : `${caught.message}${caught.traceId ? ` · ${caught.traceId}` : ""}`,
        );
      } else {
        setError(t("composer.createFailed"));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const modes: Array<{ id: ExecutionMode; label: string; enabled: boolean }> = [
    { id: "offline_fake", label: t("composer.modeOffline"), enabled: true },
    {
      id: "docker",
      label: t("composer.modeDocker"),
      enabled: Boolean(meta?.execution_modes.includes("docker")),
    },
    {
      id: "live",
      label: t("composer.modeLive"),
      enabled: Boolean(meta?.execution_modes.includes("live")),
    },
  ];

  return (
    <div className="mx-auto flex w-full max-w-[820px] flex-col px-6 pb-16 pt-[8vh]">
      <div className="mb-7">
        <div className="mb-2 flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-subtle">
          <GitBranch size={13} /> {t("composer.newSession")}
        </div>
        <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.03em]">
          {t("composer.headline")}
          <span className="text-muted"> {t("composer.headlineAccent")}</span>
        </h1>
        <p className="mt-2 max-w-[620px] text-[13px] leading-relaxed text-muted">
          {t("composer.intro")}
        </p>
      </div>

      <div className="panel overflow-hidden shadow-soft">
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
          <div className="flex items-center gap-1 rounded-lg bg-canvas p-0.5" role="group" aria-label={t("composer.executionMode")}>
            {modes.map((item) => (
              <button
                key={item.id}
                className="rounded-md px-2.5 py-1 text-[11.5px] text-muted transition-colors data-[active=true]:bg-surface data-[active=true]:text-ink data-[active=true]:shadow-sm disabled:cursor-not-allowed disabled:opacity-60"
                data-active={executionMode === item.id}
                disabled={!item.enabled}
                title={!item.enabled ? t("composer.liveDisabled", { reason: liveReason(meta, t) }) : undefined}
                onClick={() => setExecutionMode(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {live && readyModels.length ? (
              <select
                className="select h-8 w-[230px] py-0 text-[12px]"
                value={effectiveModelChoice}
                onChange={(event) => setModelChoice(event.target.value)}
                aria-label={t("composer.model")}
              >
                {readyModels.map((item) => (
                  <option key={`${item.provider_id}/${item.model_id}`} value={`${item.provider_id}/${item.model_id}`}>
                    {item.provider_name} · {item.display_name || item.model_id}
                  </option>
                ))}
              </select>
            ) : null}
            <div
              className="flex items-center gap-1.5 rounded-md border bg-elevated px-2 py-1 text-[10.5px] text-muted"
              title={live ? t("composer.liveBadgeTitle") : t("composer.fakeBadgeTitle")}
            >
              {live ? <GitBranch size={11} /> : <FlaskConical size={11} />}{" "}
              {live
                ? t("composer.liveBadge", { model: selectedModel?.display_name ?? selectedModel?.model_id ?? "" })
                : t("composer.fakeBadge")}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-b bg-canvas/40 px-4 py-2">
          <div className="flex items-center gap-1 rounded-lg bg-canvas p-0.5">
            <button
              className="rounded-md px-2.5 py-1 text-[11.5px] transition-colors data-[active=true]:bg-surface data-[active=true]:text-ink data-[active=true]:shadow-sm"
              data-active={repositoryKind === "demo"}
              onClick={() => setRepositoryKind("demo")}
            >
              {t("composer.demoRepository")}
            </button>
            <button
              className="rounded-md px-2.5 py-1 text-[11.5px] text-muted transition-colors data-[active=true]:bg-surface data-[active=true]:text-ink data-[active=true]:shadow-sm"
              data-active={repositoryKind === "https"}
              onClick={() => setRepositoryKind("https")}
            >
              {t("composer.httpsRepository")}
            </button>
          </div>
          {repositoryKind === "demo" ? (
            <div className="relative min-w-0 flex-1">
              <select
                className="select h-8 py-0 text-[12px]"
                value={effectiveDemoName}
                onChange={(event) => setDemoName(event.target.value)}
                aria-label={t("composer.demoRepositoryAria")}
              >
                {meta?.demo_repositories.map((repository) => (
                  <option key={repository.name} value={repository.name}>
                    {repository.label}
                  </option>
                ))}
                {!meta?.demo_repositories.length ? (
                  <option value="">{t("composer.loadingRepositories")}</option>
                ) : null}
              </select>
            </div>
          ) : (
            <input
              className="input h-8 min-w-0 flex-1 py-0 text-[12px]"
              value={repositoryUrl}
              onChange={(event) => setRepositoryUrl(event.target.value)}
              placeholder="https://github.com/owner/repository"
              aria-label={t("composer.repositoryUrl")}
              spellCheck={false}
            />
          )}
        </div>

        <textarea
          className="min-h-[172px] w-full resize-none bg-surface px-4 py-3.5 text-[14px] leading-relaxed text-ink outline-none placeholder:text-subtle"
          value={issue}
          onChange={(event) => setIssue(event.target.value)}
          placeholder={t("composer.issuePlaceholder")}
          maxLength={20_000}
          aria-label={t("composer.issueAria")}
          autoFocus
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") void submit();
          }}
        />

        <div className="flex flex-wrap items-center gap-2 border-t px-3 py-2.5">
          <input
            className="input h-8 w-[128px] py-0 font-mono text-[11.5px]"
            value={baseCommit}
            onChange={(event) => setBaseCommit(event.target.value)}
            aria-label={t("composer.baseCommit")}
            title={t("composer.baseCommitTitle")}
            spellCheck={false}
          />
          <button
            className="btn btn-ghost h-8 px-2 text-[11.5px]"
            onClick={() => setShowBudget((value) => !value)}
            aria-expanded={showBudget}
          >
            <SlidersHorizontal size={13} /> {t("composer.budget")}
            <ChevronDown size={12} className={showBudget ? "rotate-180 transition-transform" : "transition-transform"} />
          </button>
          <button
            className="text-[11.5px] text-muted hover:text-ink"
            onClick={() => setIssue(EXAMPLE_ISSUE)}
          >
            {t("composer.useExample")}
          </button>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden text-[10.5px] text-subtle sm:block">
              {t("composer.chars", { count: issue.trim().length })}
            </span>
            <Button
              variant="primary"
              onClick={() => void submit()}
              disabled={!canSubmit}
              loading={submitting}
              icon={<ArrowRight size={15} />}
            >
              {t("composer.start")}
            </Button>
          </div>
        </div>

        {showBudget ? (
          <div className="grid grid-cols-2 gap-3 border-t bg-canvas/60 px-4 py-3 sm:grid-cols-4">
            <label className="text-[10.5px] text-muted">
              {t("composer.iterations")}
              <input
                className="input mt-1 h-8 py-0 text-[12px]"
                type="number"
                min={1}
                max={10}
                value={maxIterations}
                onChange={(event) => setMaxIterations(event.target.value)}
              />
            </label>
            <label className="text-[10.5px] text-muted">
              {t("composer.testRuns")}
              <input
                className="input mt-1 h-8 py-0 text-[12px]"
                type="number"
                min={3}
                max={60}
                value={maxTestRuns}
                onChange={(event) => setMaxTestRuns(event.target.value)}
              />
            </label>
            <label className="text-[10.5px] text-muted">
              {t("composer.runtime")}
              <input
                className="input mt-1 h-8 py-0 text-[12px]"
                type="number"
                min={30}
                max={7200}
                value={maxRuntime}
                onChange={(event) => setMaxRuntime(event.target.value)}
              />
            </label>
            <label className="text-[10.5px] text-muted">
              {live ? t("composer.maxCostRequired") : t("composer.maxCost")}
              <input
                className="input mt-1 h-8 py-0 text-[12px]"
                type="number"
                min={0}
                step="0.1"
                value={maxCost}
                onChange={(event) => setMaxCost(event.target.value)}
                placeholder={live ? "0.50" : t("composer.unlimited")}
              />
            </label>
          </div>
        ) : null}
      </div>

      {live ? (
        <div className="mt-3">
          <Notice tone="warning">{t("composer.liveWarning")}</Notice>
        </div>
      ) : null}
      {!live && !meta?.live_llm_enabled ? (
        <div className="mt-3 text-[10.5px] text-subtle">{t("composer.simulatedNote")}</div>
      ) : null}
      {urlError ? (
        <div className="mt-3">
          <Notice tone="danger">{urlError}</Notice>
        </div>
      ) : null}
      {error ? (
        <div className="mt-3">
          <Notice tone="danger">{error}</Notice>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-start gap-x-5 gap-y-2 text-[11px] leading-relaxed text-subtle">
        <span className="inline-flex items-center gap-1.5">
          <LockKeyhole size={12} /> {t("composer.apiKeyLocal")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <GitBranch size={12} /> {t("composer.allowList", { hosts: allowedHosts.join(", ") || "—" })}
        </span>
      </div>
    </div>
  );
}
