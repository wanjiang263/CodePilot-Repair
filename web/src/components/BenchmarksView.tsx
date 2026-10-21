import { ArrowUpRight, FlaskConical, ShieldCheck, ShieldX } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { HistoryItem } from "../api/types";
import { formatDate } from "../lib/format";
import { verificationKey } from "../lib/status";
import { EmptyState } from "./ui/EmptyState";
import { StatusBadge } from "./ui/StatusBadge";

export function BenchmarksView({
  benchmarks,
  loading,
  onOpenBenchmark,
}: {
  benchmarks: HistoryItem[];
  loading: boolean;
  onOpenBenchmark: (benchId: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mx-auto w-full max-w-[1080px] px-6 py-8">
      <div className="mb-6">
        <div className="mb-1 flex items-center gap-1.5 text-[10.5px] font-medium uppercase tracking-[0.14em] text-subtle">
          <FlaskConical size={13} /> {t("benchmarks.evaluation")}
        </div>
        <h1 className="text-[24px] font-semibold tracking-[-0.03em]">{t("benchmarks.title")}</h1>
        <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-muted">
          {t("benchmarks.intro")}
        </p>
      </div>

      {loading ? (
        <div className="panel space-y-3 p-4">
          <div className="skeleton h-4 w-1/3" />
          <div className="skeleton h-4 w-2/3" />
        </div>
      ) : null}

      {!loading && !benchmarks.length ? (
        <div className="panel">
          <EmptyState
            icon={<FlaskConical size={16} />}
            title={t("benchmarks.none")}
            description={t("benchmarks.noneDescription")}
          />
        </div>
      ) : null}

      {!loading && benchmarks.length ? (
        <div className="space-y-2">
          {benchmarks.map((bench) => {
            const officialOk = bench.official_verdict?.toUpperCase() === "RESOLVED";
            return (
              <button
                key={bench.history_id}
                className="panel group flex w-full items-start gap-4 px-4 py-4 text-left transition-colors hover:bg-elevated/50"
                onClick={() => onOpenBenchmark(bench.history_id)}
              >
                <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border bg-elevated">
                  {officialOk ? (
                    <ShieldCheck size={15} className="text-success" />
                  ) : (
                    <ShieldX size={15} className="text-muted" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium">{bench.label}</span>
                    <StatusBadge status={bench.ui_status} stopReason={bench.stop_reason} compact />
                  </div>
                  <div className="mt-1 text-[10.5px] text-subtle">
                    {bench.instance_id} · {bench.model} · {formatDate(bench.recorded_at)} ·{" "}
                    {t("benchmarks.events", { count: bench.event_count })}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[9.5px]">
                    <span className="rounded border bg-canvas px-1.5 py-0.5 text-muted">
                      {t("benchmarks.official", { value: bench.official_verdict ?? t("common.notRecorded") })}
                    </span>
                    <span className="rounded border bg-canvas px-1.5 py-0.5 text-muted">
                      {t("benchmarks.internal", { value: t(verificationKey(bench.verification_level)) })}
                    </span>
                    {bench.stop_reason ? (
                      <span className="rounded border bg-canvas px-1.5 py-0.5 text-warning">
                        {t("benchmarks.stop", { value: bench.stop_reason })}
                      </span>
                    ) : null}
                  </div>
                </div>
                <ArrowUpRight size={15} className="mt-1 text-subtle group-hover:text-ink" />
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
