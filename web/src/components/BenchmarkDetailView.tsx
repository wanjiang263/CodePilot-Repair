import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  CircleSlash,
  Cpu,
  Database,
  FlaskConical,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, getBenchmark } from "../api/client";
import type { Benchmark } from "../api/types";
import { formatCost, formatDate, formatNumber } from "../lib/format";
import { verificationKey } from "../lib/status";
import { Button } from "./ui/Button";
import { Notice } from "./ui/Notice";
import { StatusBadge } from "./ui/StatusBadge";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[150px_1fr] gap-3 border-b py-2 text-[11.5px] last:border-b-0">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 leading-relaxed">{children}</span>
    </div>
  );
}

export function BenchmarkDetailView({
  benchId,
  apiKey,
  onBack,
  onOpenReplay,
}: {
  benchId: string;
  apiKey: string;
  onBack: () => void;
  onOpenReplay: (benchId: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const detailKey = `${apiKey}|${benchId}|${i18n.language}`;
  const [lastDetailKey, setLastDetailKey] = useState(detailKey);

  // Flip back to the loading state synchronously during render when the
  // benchmark identity changes (the sanctioned "adjust state" pattern).
  if (lastDetailKey !== detailKey) {
    setLastDetailKey(detailKey);
    setLoading(true);
  }

  useEffect(() => {
    const controller = new AbortController();
    getBenchmark(apiKey, benchId, controller.signal)
      .then(setBenchmark)
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setError(caught instanceof ApiError ? caught.message : t("benchmarkDetail.loadFailed"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [apiKey, benchId, t]);

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-[1000px] space-y-3 px-6 py-8">
        <div className="skeleton h-6 w-1/3" />
        <div className="skeleton h-24 w-full" />
        <div className="skeleton h-40 w-full" />
      </div>
    );
  }
  if (error || !benchmark) {
    return (
      <div className="mx-auto max-w-[800px] px-6 py-8">
        <Button size="sm" variant="ghost" onClick={onBack} icon={<ArrowLeft size={13} />}>
          {t("benchmarkDetail.benchmarksBack")}
        </Button>
        <div className="mt-4">
          <Notice tone="danger">{error ?? t("benchmarkDetail.notFound")}</Notice>
        </div>
      </div>
    );
  }

  const official = benchmark.official;
  const internal = benchmark.internal;
  const closure = internal.closure ?? {};
  const harness = benchmark.harness ?? {};
  const tests = benchmark.tests ?? {};
  const review = benchmark.review ?? {};
  const integrityOk = benchmark.integrity.every((check) => check.ok);

  return (
    <div className="mx-auto w-full max-w-[1000px] px-6 py-7">
      <button className="mb-4 inline-flex items-center gap-1 text-[11px] text-muted hover:text-ink" onClick={onBack}>
        <ArrowLeft size={13} /> {t("benchmarkDetail.benchmarksBack")}
      </button>

      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="mb-1 flex items-center gap-2 text-[10.5px] uppercase tracking-[0.14em] text-subtle">
            <FlaskConical size={13} /> {t("benchmarkDetail.replayReadOnly")}
          </div>
          <h1 className="text-[23px] font-semibold tracking-[-0.03em]">{benchmark.label}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-subtle">
            <span className="font-mono">{benchmark.instance_id}</span>
            <span>·</span>
            <span>{benchmark.model}</span>
            <span>·</span>
            <span>{formatDate(benchmark.recorded_at)}</span>
            <StatusBadge status={benchmark.ui_status} stopReason={benchmark.stop_reason} compact />
          </div>
        </div>
        <Button size="sm" variant="primary" onClick={() => onOpenReplay(benchmark.bench_id)} icon={<ArrowUpRight size={13} />}>
          {t("benchmarkDetail.openReplay")}
        </Button>
      </div>

      <div className="mb-4 grid gap-3 md:grid-cols-2">
        <div className="panel px-4 py-2">
          <div className="mb-1 flex items-center gap-1.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            <ShieldCheck size={12} /> {t("benchmarkDetail.officialVerdict")}
          </div>
          <div className="pb-2 pt-1">
            <div className="text-[24px] font-semibold tracking-[-0.03em]">
              {official.verdict ?? (official.recorded ? t("benchmarkDetail.unknown") : t("benchmarkDetail.notRecorded"))}
            </div>
            <div className="mt-0.5 text-[10.5px] text-subtle">
              {official.harness_status ?? t("benchmarkDetail.noHarnessStatus")} · {t("benchmarkDetail.officialAuthoritative")}
            </div>
          </div>
          <Row label={t("benchmarkDetail.harnessStatus")}>{benchmark.harness.status ?? t("common.notRecorded")}</Row>
          <Row label={t("benchmarkDetail.decision")}>{harness.decision ?? t("common.notRecorded")}</Row>
          <Row label={t("benchmarkDetail.exitCode")}>{harness.exit_code ?? "—"}</Row>
          <Row label={t("benchmarkDetail.harnessRuntime")}>
            {typeof harness.runtime_seconds === "number" ? `${harness.runtime_seconds.toFixed(1)} s` : "—"}
          </Row>
          {official.note ? <Row label={t("benchmarkDetail.officialNote")}>{official.note}</Row> : null}
        </div>

        <div className="panel px-4 py-2">
          <div className="mb-1 flex items-center gap-1.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            <Cpu size={12} /> {t("benchmarkDetail.internalVerification")}
          </div>
          <div className="pb-2 pt-1">
            <div className="text-[24px] font-semibold tracking-[-0.03em]">
              {t(verificationKey(benchmark.internal.verification_level))}
            </div>
            <div className="mt-0.5 text-[10.5px] text-subtle">{t("benchmarkDetail.internalHint")}</div>
          </div>
          <Row label={t("benchmarkDetail.rawStatus")}>{benchmark.status}</Row>
          <Row label={t("benchmarkDetail.stopReason")}>{benchmark.stop_reason ?? t("final.none")}</Row>
          <Row label={t("benchmarkDetail.reviewer")}>
            {review.executed
              ? review.approved
                ? t("benchmarkDetail.approved")
                : t("benchmarkDetail.notApproved")
              : t("common.notExecuted")}
          </Row>
          <Row label={t("benchmarkDetail.reviewBound")}>{String(review.bound_to_candidate ?? t("common.notRecorded"))}</Row>
          <Row label={t("benchmarkDetail.regression")}>
            {tests.regression
              ? `${tests.regression.complete ? t("benchmarkDetail.complete") : t("benchmarkDetail.incomplete")} · ${(tests.regression.levels ?? []).join(" / ") || t("benchmarkDetail.noLevels")}`
              : t("common.notExecuted")}
          </Row>
        </div>
      </div>

      <div className="mb-4 grid gap-3 md:grid-cols-2">
        <div className="panel px-4 py-2">
          <div className="mb-1 flex items-center gap-1.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            <Database size={12} /> {t("benchmarkDetail.usageBudget")}
          </div>
          <Row label={t("benchmarkDetail.tokens")}>
            {formatNumber(benchmark.usage.input_tokens)} / {formatNumber(benchmark.usage.output_tokens)} /{" "}
            {formatNumber(benchmark.usage.cached_input_tokens)}
          </Row>
          <Row label={t("benchmarkDetail.cost")}>
            {formatCost(benchmark.usage.cost, benchmark.usage.currency)} · {t("benchmarkDetail.llmCalls", { count: benchmark.usage.llm_calls })}
          </Row>
          <Row label={t("benchmarkDetail.budgetUsage")}>
            {Object.entries((closure.budget_usage ?? {}) as Record<string, string>)
              .map(([key, value]) => `${key}=${value}`)
              .join(" · ") || t("common.notRecorded")}
          </Row>
          <Row label={t("benchmarkDetail.testRuns")}>{tests.test_runs ?? t("common.notRecorded")}</Row>
          <Row label={t("benchmarkDetail.costKnown")}>
            {benchmark.usage.cost_known ? t("benchmarkDetail.costKnownYes") : t("benchmarkDetail.costUnknownPrice")}
          </Row>
        </div>

        <div className="panel px-4 py-2">
          <div className="mb-1 flex items-center gap-1.5 pt-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            <Check size={12} /> {t("benchmarkDetail.artifactIntegrity")}
          </div>
          <Row label={t("common.status")}>{integrityOk ? t("benchmarkDetail.allChecks") : t("benchmarkDetail.attention")}</Row>
          <div className="space-y-1.5 py-2">
            {benchmark.integrity.map((check) => (
              <div key={check.name} className="flex items-start gap-2 text-[10.5px]">
                <span className={check.ok ? "text-success" : "text-danger"}>
                  {check.ok ? <Check size={11} /> : <X size={11} />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="font-mono">{check.name}</span>
                  <span className="ml-1 text-subtle">{check.detail}</span>
                </span>
              </div>
            ))}
          </div>
          {benchmark.note ? <Notice tone="info">{benchmark.note}</Notice> : null}
        </div>
      </div>

      {benchmark.internal.unexecuted_stages.length ? (
        <div className="panel px-4 py-3">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            <CircleSlash size={12} /> {t("benchmarkDetail.stagesNotExecuted")}
          </div>
          <ul className="space-y-1 text-[11px] text-muted">
            {benchmark.internal.unexecuted_stages.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-4 flex justify-end">
        <Button variant="primary" size="sm" onClick={() => onOpenReplay(benchmark.bench_id)}>
          {t("benchmarkDetail.replayTimeline")}
        </Button>
      </div>
    </div>
  );
}
