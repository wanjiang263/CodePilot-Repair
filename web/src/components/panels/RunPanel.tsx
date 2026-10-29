import { clsx } from "clsx";
import { Boxes, ChevronRight, Gauge, Timer } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../../hooks/useSessionData";
import { formatCost, formatNumber, percent } from "../../lib/format";
import { stopReasonKey, verificationKey } from "../../lib/status";
import { EmptyState } from "../ui/EmptyState";
import { HarnessStatus } from "./HarnessStatus";

function ratio(value: string | undefined): { used: number; limit: number | null; raw: string } | null {
  if (!value) return null;
  const [usedRaw, limitRaw] = value.split("/");
  const used = Number.parseFloat(usedRaw);
  if (Number.isNaN(used)) return { used: 0, limit: null, raw: value };
  const limit =
    limitRaw && limitRaw.trim() !== "unlimited"
      ? Number.parseFloat(limitRaw.replace("e+", "e"))
      : null;
  return { used, limit: limit !== null && Number.isFinite(limit) ? limit : null, raw: value };
}

function Meter({
  label,
  value,
  detail,
}: {
  label: string;
  value: string | undefined;
  detail?: string;
}) {
  const { t } = useTranslation();
  const parsed = ratio(value);
  if (!parsed) {
    return (
      <div className="flex items-center justify-between gap-3 py-1.5">
        <span className="text-[11.5px] text-muted">{label}</span>
        <span className="text-[11px] text-subtle">{t("common.notRecorded")}</span>
      </div>
    );
  }
  const width = percent(parsed.used, parsed.limit);
  const tone =
    width === null ? "neutral" : width >= 100 ? "danger" : width >= 80 ? "warning" : "accent";
  return (
    <div className="py-1.5">
      <div className="flex items-center justify-between gap-3 text-[11.5px]">
        <span className="text-muted">{label}</span>
        <span className="font-mono text-[10.5px]">{parsed.raw}</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-line/70">
        <div
          className={clsx(
            "h-full rounded-full",
            tone === "accent" && "bg-accent",
            tone === "warning" && "bg-warning",
            tone === "danger" && "bg-danger",
            tone === "neutral" && "bg-subtle",
          )}
          style={{ width: `${width ?? 0}%` }}
        />
      </div>
      {detail ? <div className="mt-1 text-[10px] text-subtle">{detail}</div> : null}
    </div>
  );
}

export function RunPanel({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const report = session.report;
  const usage = session.usage;
  const budget = session.budget ?? {};
  const closure = report?.internal.closure ?? {};
  const budgetUsage = (closure.budget_usage ?? {}) as Record<string, string>;
  const admission = closure.closure_admission as Record<string, unknown> | undefined;
  const maxCost = typeof budget.max_cost === "number" ? budget.max_cost : null;
  const remaining = maxCost !== null ? Math.max(0, maxCost - usage.cost) : null;
  const maxTokens = typeof budget.max_total_tokens === "number" ? budget.max_total_tokens : null;
  const reasonKey = stopReasonKey(session.stopReason);

  const nodeDurations = useMemo(() => {
    const buckets = new Map<string, number>();
    for (const event of session.events) {
      if (typeof event.duration_ms !== "number") continue;
      buckets.set(event.node, (buckets.get(event.node) ?? 0) + event.duration_ms);
    }
    return [...buckets.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  }, [session.events]);

  if (!report && !session.task) {
    return <EmptyState title={t("panels.noReport")} description={t("panels.noReportDescription")} />;
  }

  const artifacts = Object.entries((report?.artifacts ?? {}) as Record<string, unknown>).filter(
    ([key, value]) => key !== "run_dir" && value !== null,
  );

  return (
    <div className="pb-4">
      <HarnessStatus session={session} />
      <div className="border-b px-3 py-3">
        <div className="mb-2 flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
          <Gauge size={12} /> {t("panels.budgetUsage")}
        </div>
        <Meter
          label={t("panels.totalTokens")}
          value={budgetUsage.tokens}
          detail={`${formatNumber(usage.input_tokens)} in · ${formatNumber(usage.output_tokens)} out · ${formatNumber(usage.cached_input_tokens)} cached`}
        />
        <Meter
          label={t("panels.cost")}
          value={budgetUsage.cost}
          detail={`${t("panels.costUsed", { cost: formatCost(usage.cost, usage.currency) })}${
            remaining !== null
              ? ` · ${t("panels.remaining", { cost: formatCost(remaining, usage.currency) })}`
              : ` · ${t("panels.noHardCap")}`
          }`}
        />
        <Meter label={t("panels.testRuns")} value={budgetUsage.test_runs} />
        <Meter label={t("panels.toolCalls")} value={budgetUsage.tool_calls} />
        <Meter label={t("panels.reviewCallsBudget")} value={budgetUsage.review_calls} />
        <Meter label={t("panels.iterations")} value={budgetUsage.iterations} />
        <Meter label={t("panels.wallClock")} value={budgetUsage.time} />
        {maxTokens !== null ? (
          <div className="mt-1 flex items-center justify-between text-[10.5px] text-subtle">
            <span>{t("panels.tokenCeiling")}</span>
            <span className="font-mono">{formatNumber(maxTokens)}</span>
          </div>
        ) : null}
        <div className="mt-2 grid grid-cols-2 gap-2 border-t pt-2 text-[10.5px]">
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.llmCalls")}</span>
            <span className="font-mono">{usage.llm_calls}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.costKnown")}</span>
            <span className="font-mono">{usage.cost_known ? t("common.yes") : t("common.no")}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.testRuns")}</span>
            <span className="font-mono">{report?.tests.test_runs ?? t("common.notRecorded")}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.attempts")}</span>
            <span className="font-mono">{session.attempts}</span>
          </div>
        </div>
      </div>

      <div className="border-b px-3 py-3">
        <div className="mb-2 flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
          <Timer size={12} /> {t("panels.agentNodeTime")}
        </div>
        {nodeDurations.length ? (
          <div className="space-y-1">
            {nodeDurations.map(([node, ms]) => (
              <div key={node} className="flex items-center gap-2 text-[10.5px]">
                <span className="min-w-0 flex-1 truncate text-muted">{node.replaceAll("_", " ")}</span>
                <span className="font-mono text-subtle">{(ms / 1000).toFixed(1)}s</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-[10.5px] text-subtle">{t("panels.notRecordedEventLog")}</div>
        )}
      </div>

      <div className="border-b px-3 py-3">
        <div className="mb-2 flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
          <Boxes size={12} /> {t("panels.closureStop")}
        </div>
        <div className="space-y-1.5 text-[11px]">
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.verificationLevel")}</span>
            <span className="text-right">{t(verificationKey(report?.verification_level ?? session.verificationLevel))}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.stopReason")}</span>
            <span className="text-right">
              {session.stopReason ? (reasonKey ? t(reasonKey) : session.stopReason) : t("panels.none")}
              {session.stopReason && reasonKey ? (
                <span className="ml-1 font-mono text-[9.5px] text-subtle">{session.stopReason}</span>
              ) : null}
            </span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.codingStop")}</span>
            <span className="text-right font-mono text-[10px]">
              {String(closure.coding_stop_reason ?? t("panels.none"))}
            </span>
          </div>
          {admission ? (
            <div className="rounded-md border bg-canvas/60 px-2 py-1.5">
              <div className="flex justify-between gap-2">
                <span className="text-muted">{t("panels.closureAdmission")}</span>
                <span>{admission.feasible ? t("panels.admissionFeasible") : t("panels.admissionRefused")}</span>
              </div>
              <div className="mt-0.5 text-[9.5px] text-subtle">
                {t("panels.admissionDetail", {
                  required: String(admission.required),
                  remaining: String(admission.remaining),
                  share: String(admission.coder_share),
                  trigger: String(admission.trigger),
                })}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="px-3 py-3">
        <div className="mb-2 flex items-center gap-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
          <ChevronRight size={12} /> {t("panels.artifactsIdentity")}
        </div>
        <div className="space-y-1 text-[10.5px]">
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.runId")}</span>
            <span className="truncate font-mono text-[9.5px]" title={session.runId ?? undefined}>
              {session.runId ?? "—"}
            </span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.baseCommit")}</span>
            <span className="font-mono text-[9.5px]">{session.baseCommit || "—"}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.simulation")}</span>
            <span>
              {session.simulatedLlm ? "FakeLLM" : t("panels.liveModel")} /{" "}
              {session.simulatedSandbox ? t("panels.fakeSandbox") : t("panels.sandbox")}
            </span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.llmMode")}</span>
            <span className="font-mono text-[10px]">{session.task?.llm_mode ?? "fake"}</span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.modelIdentity")}</span>
            <span className="truncate text-right font-mono text-[9.5px]" title={session.task?.model_identity ?? undefined}>
              {session.task?.model_identity ?? session.task?.model_id ?? "FakeLLM"}
            </span>
          </div>
          <div className="flex justify-between gap-2">
            <span className="text-muted">{t("panels.configHash")}</span>
            <span className="truncate font-mono text-[9.5px]" title={session.task?.config_hash ?? undefined}>
              {session.task?.config_hash?.slice(0, 16) ?? "—"}
            </span>
          </div>
        </div>
        <div className="mt-2 space-y-0.5">
          {artifacts.length ? (
            artifacts.slice(0, 16).map(([name, value]) => (
              <div key={name} className="flex justify-between gap-2 text-[10px] text-muted">
                <span className="truncate font-mono">{name}</span>
                <span className="text-subtle">
                  {typeof value === "object" && value && "size" in value
                    ? `${formatNumber(Number((value as { size?: number }).size ?? 0))} B`
                    : t("panels.recorded")}
                </span>
              </div>
            ))
          ) : (
            <div className="text-[10px] text-subtle">{t("panels.artifactsFinalize")}</div>
          )}
        </div>
      </div>
    </div>
  );
}
