import { ArrowUpRight, Filter, History, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { HistoryItem } from "../api/types";
import { formatDate } from "../lib/format";
import { verificationKey } from "../lib/status";
import { EmptyState } from "./ui/EmptyState";
import { StatusBadge } from "./ui/StatusBadge";

export function HistoryView({
  history,
  loading,
  onOpenReplay,
}: {
  history: HistoryItem[];
  loading: boolean;
  onOpenReplay: (benchId: string) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [model, setModel] = useState("all");
  const models = useMemo(() => [...new Set(history.map((item) => item.model))], [history]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return history.filter((item) => {
      if (status !== "all" && item.ui_status !== status) return false;
      if (model !== "all" && item.model !== model) return false;
      if (!needle) return true;
      return [item.label, item.instance_id, item.repository, item.model, item.note ?? ""]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [history, model, query, status]);

  return (
    <div className="mx-auto w-full max-w-[1080px] px-6 py-8">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[10.5px] font-medium uppercase tracking-[0.14em] text-subtle">
            <History size={13} /> {t("history.replayArchive")}
          </div>
          <h1 className="text-[24px] font-semibold tracking-[-0.03em]">{t("history.title")}</h1>
          <p className="mt-1 max-w-xl text-[12.5px] leading-relaxed text-muted">
            {t("history.intro")}
          </p>
        </div>
        <div className="rounded-lg border bg-surface px-3 py-2 text-[10.5px] text-muted">
          <div className="font-mono text-[16px] font-semibold text-ink">{history.length}</div>
          {t("history.archived", { count: history.length })}
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-subtle" />
          <input
            className="input h-9 pl-8"
            placeholder={t("history.searchPlaceholder")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label={t("history.searchPlaceholder")}
          />
        </div>
        <div className="flex items-center gap-1.5">
          <Filter size={12} className="text-subtle" />
          <select
            className="select h-9 w-[136px] py-0 text-[12px]"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label={t("history.filterStatus")}
          >
            <option value="all">{t("history.allStatuses")}</option>
            <option value="completed">{t("status.completed.label")}</option>
            <option value="stopped">{t("status.stopped.label")}</option>
            <option value="failed">{t("status.failed.label")}</option>
            <option value="error">{t("status.error.label")}</option>
            <option value="cancelled">{t("status.cancelled.label")}</option>
          </select>
          <select
            className="select h-9 w-[150px] py-0 text-[12px]"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            aria-label={t("history.filterModel")}
          >
            <option value="all">{t("history.allModels")}</option>
            {models.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <div className="panel divide-y">
          {[0, 1, 2].map((index) => (
            <div key={index} className="space-y-2 px-4 py-4">
              <div className="skeleton h-3 w-1/3" />
              <div className="skeleton h-3 w-2/3" />
            </div>
          ))}
        </div>
      ) : null}

      {!loading && !filtered.length ? (
        <div className="panel">
          <EmptyState
            icon={<History size={16} />}
            title={history.length ? t("history.noMatch") : t("history.noHistory")}
            description={
              history.length
                ? t("history.noMatchDescription")
                : t("history.noHistoryDescription")
            }
          />
        </div>
      ) : null}

      {!loading && filtered.length ? (
        <div className="panel divide-y overflow-hidden">
          {filtered.map((item) => (
            <button
              key={item.history_id}
              className="group flex w-full items-start gap-3 px-4 py-3.5 text-left transition-colors hover:bg-elevated/60"
              onClick={() => onOpenReplay(item.history_id)}
            >
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[13px] font-medium">{item.label}</span>
                  <StatusBadge status={item.ui_status} stopReason={item.stop_reason} compact />
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-subtle">
                  <span className="font-mono">{item.instance_id || item.run_id}</span>
                  <span>·</span>
                  <span>{item.model}</span>
                  <span>·</span>
                  <span>{formatDate(item.recorded_at)}</span>
                  <span>·</span>
                  <span>{t("history.events", { count: item.event_count })}</span>
                  {item.stop_reason ? (
                    <>
                      <span>·</span>
                      <span className="text-warning">{item.stop_reason}</span>
                    </>
                  ) : null}
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <span className="rounded border bg-canvas px-1.5 py-0.5 text-[9.5px] text-muted">
                    {t("history.official", { value: item.official_verdict ?? t("common.notRecorded") })}
                  </span>
                  <span className="rounded border bg-canvas px-1.5 py-0.5 text-[9.5px] text-muted">
                    {t("history.internal", { value: t(verificationKey(item.verification_level)) })}
                  </span>
                  {item.note ? (
                    <span className="truncate text-[9.5px] text-subtle">{item.note}</span>
                  ) : null}
                </div>
              </div>
              <ArrowUpRight
                size={15}
                className="mt-1 shrink-0 text-subtle transition-colors group-hover:text-ink"
              />
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
