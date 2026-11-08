import { Gauge, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { updateSettingsSection } from "../../api/client";
import type { ExecutionSettings as ExecutionShape, SettingsDocument } from "../../api/types";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Notice";
import { Field, SettingsSection } from "./shared";

type Execute = (
  action: () => Promise<SettingsDocument>,
  successText?: string,
) => Promise<SettingsDocument | null>;

interface Draft {
  max_iterations: string;
  max_test_runs: string;
  max_files_changed: string;
  max_lines_changed: string;
  max_tool_calls_per_iteration: string;
  max_review_calls: string;
  max_runtime_seconds: string;
  max_total_tokens: string;
  max_cost: string;
  max_parallel_tasks: string;
  budget_currency: string;
}

function toDraft(execution: ExecutionShape): Draft {
  return {
    max_iterations: String(execution.max_iterations),
    max_test_runs: String(execution.max_test_runs),
    max_files_changed: String(execution.max_files_changed),
    max_lines_changed: String(execution.max_lines_changed),
    max_tool_calls_per_iteration: String(execution.max_tool_calls_per_iteration),
    max_review_calls: String(execution.max_review_calls),
    max_runtime_seconds: String(execution.max_runtime_seconds),
    max_total_tokens: String(execution.max_total_tokens),
    max_cost: execution.max_cost === null ? "" : String(execution.max_cost),
    max_parallel_tasks: String(execution.max_parallel_tasks),
    budget_currency: execution.budget_currency,
  };
}

export function BudgetSettings({
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
  const [draft, setDraft] = useState<Draft>(toDraft(document.execution));
  const [rates, setRates] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(document.execution.exchange_rates).map(([key, value]) => [key, String(value)])),
  );
  const [newCurrency, setNewCurrency] = useState("");
  const [newRate, setNewRate] = useState("");
  const [lastExecution, setLastExecution] = useState(document.execution);

  // Re-seed the draft and rates synchronously during render when the server
  // document changes (the sanctioned "adjust state when a prop changes" pattern).
  if (lastExecution !== document.execution) {
    setLastExecution(document.execution);
    setDraft(toDraft(document.execution));
    setRates(
      Object.fromEntries(Object.entries(document.execution.exchange_rates).map(([key, value]) => [key, String(value)])),
    );
  }

  const save = async () => {
    const numericKeys: Array<keyof Draft> = [
      "max_iterations",
      "max_test_runs",
      "max_files_changed",
      "max_lines_changed",
      "max_tool_calls_per_iteration",
      "max_review_calls",
      "max_runtime_seconds",
      "max_total_tokens",
      "max_parallel_tasks",
    ];
    if (numericKeys.some((key) => !Number.isFinite(Number(draft[key])) || Number(draft[key]) <= 0)) {
      return;
    }
    await execute(
      () =>
        updateSettingsSection(
          apiKey,
          "execution",
          {
            ...document.execution,
            ...Object.fromEntries(numericKeys.map((key) => [key, Number(draft[key])])),
            max_cost: draft.max_cost.trim() ? Number(draft.max_cost) : null,
            budget_currency: draft.budget_currency.toUpperCase(),
            exchange_rates: Object.fromEntries(
              Object.entries(rates)
                .filter(([code]) => /^[A-Z]{3}$/.test(code.toUpperCase()))
                .map(([code, value]) => [code.toUpperCase(), Number(value)]),
            ),
          },
          document.version,
        ),
      t("settings.saved", { version: document.version + 1 }),
    );
  };

  const fields: Array<{ key: keyof Draft; label: string; placeholder?: string }> = [
    { key: "max_iterations", label: t("settings.budget.maxIterations") },
    { key: "max_test_runs", label: t("settings.budget.maxTestRuns") },
    { key: "max_files_changed", label: t("settings.budget.maxFiles") },
    { key: "max_lines_changed", label: t("settings.budget.maxLines") },
    { key: "max_tool_calls_per_iteration", label: t("settings.budget.maxToolCalls") },
    { key: "max_review_calls", label: t("settings.budget.maxReviewCalls") },
    { key: "max_runtime_seconds", label: t("settings.budget.maxRuntime") },
    { key: "max_total_tokens", label: t("settings.budget.maxTokens") },
    { key: "max_cost", label: t("settings.budget.maxCost"), placeholder: t("composer.unlimited") },
    { key: "max_parallel_tasks", label: t("settings.budget.parallel") },
  ];

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.budget.title")}
        description={t("settings.budget.intro")}
        scope="newTasks"
        actions={<Gauge size={15} className="text-subtle" />}
      >
        <div className="grid gap-3 sm:grid-cols-3">
          {fields.map((field) => (
            <Field key={field.key} label={field.label}>
              <input
                className="input h-9 text-[12px]"
                type="number"
                min={0}
                step={field.key === "max_cost" ? "0.1" : "1"}
                value={draft[field.key]}
                placeholder={field.placeholder}
                onChange={(event) => setDraft({ ...draft, [field.key]: event.target.value })}
              />
            </Field>
          ))}
          <Field label={t("settings.budget.budgetCurrency")}>
            <input
              className="input h-9 font-mono text-[12px]"
              value={draft.budget_currency}
              onChange={(event) => setDraft({ ...draft, budget_currency: event.target.value.toUpperCase() })}
            />
          </Field>
        </div>
        <div className="mt-3">
          <Notice tone="info">{t("settings.budget.hardCaps")}</Notice>
        </div>
      </SettingsSection>

      <SettingsSection
        title={t("settings.budget.exchangeRates")}
        description={t("settings.budget.exchangeRatesHint")}
        scope="newTasks"
      >
        <div className="space-y-2">
          {Object.entries(rates).map(([currency, value]) => (
            <div key={currency} className="flex items-center gap-2">
              <span className="w-16 font-mono text-[11px]">{currency}</span>
              <span className="text-[10.5px] text-subtle">
                {t("settings.budget.currencyRate", { currency, base: draft.budget_currency })}
              </span>
              <input
                className="input h-8 w-32 text-[11.5px]"
                type="number"
                min={0}
                step="0.01"
                value={value}
                onChange={(event) => setRates({ ...rates, [currency]: event.target.value })}
                aria-label={`${currency} rate`}
              />
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-2 border-t pt-2">
            <Field label={t("settings.models.currency")}>
              <input
                className="input h-8 w-24 font-mono text-[11.5px]"
                value={newCurrency}
                maxLength={3}
                onChange={(event) => setNewCurrency(event.target.value.toUpperCase())}
                placeholder="USD"
              />
            </Field>
            <Field label={t("settings.budget.exchangeRates")}>
              <input
                className="input h-8 w-32 text-[11.5px]"
                type="number"
                min={0}
                step="0.01"
                value={newRate}
                onChange={(event) => setNewRate(event.target.value)}
              />
            </Field>
            <Button
              size="sm"
              variant="ghost"
              icon={<Plus size={12} />}
              disabled={!/^[A-Z]{3}$/.test(newCurrency) || !Number(newRate)}
              onClick={() => {
                setRates({ ...rates, [newCurrency]: newRate });
                setNewCurrency("");
                setNewRate("");
              }}
            >
              {t("settings.budget.addRate")}
            </Button>
          </div>
        </div>
      </SettingsSection>

      <div className="flex justify-end">
        <Button variant="primary" loading={busy} onClick={() => void save()}>
          {t("settings.save")}
        </Button>
      </div>
    </div>
  );
}
