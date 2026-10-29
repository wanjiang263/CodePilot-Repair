import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../../hooks/useSessionData";

import { mergeHarness } from "./harnessProjection";

export function HarnessStatus({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const projection = useMemo(() => mergeHarness(session), [session]);
  const [now, setNow] = useState(Date.now);
  const live = session.mode === "live" && ["running", "queued"].includes(session.status);
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live, session.runId, session.taskId]);
  const harness = { ...projection.harness };
  const runtime = { ...projection.runtime };
  if (live && runtime.activity !== "idle" && runtime.stage_started_at) {
    const start = Date.parse(runtime.stage_started_at);
    if (Number.isFinite(start)) runtime.stage_elapsed_seconds = Math.max(runtime.stage_elapsed_seconds ?? 0, (now - start) / 1000, 0);
  }
  const verification = runtime.verification ?? harness.verification ?? {};
  harness.contract = runtime.contract ?? harness.contract;
  harness.failure_summary = runtime.failure_summary ?? harness.failure_summary;
  const policy = runtime.policy_snapshot ?? harness.policy_snapshot ?? {};
  const cancellation =
    session.status === "cancelled"
      ? "confirmed"
      : session.cancelRequested || session.cancelling
        ? "requested"
        : session.task?.cancellation_state ?? "not_requested";
  const seconds = (value: unknown) => typeof value === "number" ? `${value.toFixed(1)} s` : t("common.notRecorded");
  return (
    <section className="border-b px-3 py-3 text-[11px]" aria-label={t("harness.title")}>
      <div className="mb-2 font-semibold">{t("harness.title")}</div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt>{t("harness.stage")}</dt><dd>{runtime.stage ?? t("common.notRecorded")}</dd>
        <dt>{t("harness.activity")}</dt><dd>{runtime.activity ? String(t(`harness.${runtime.activity}`)) : session.status === "queued" ? t("harness.queued") : t("common.notRecorded")}</dd>
        <dt>{t("harness.started")}</dt><dd>{runtime.stage_started_at ?? t("common.notRecorded")}</dd>
        <dt>{t("harness.elapsed")}</dt><dd>{seconds(runtime.stage_elapsed_seconds)}</dd>
        <dt>{t("harness.queueTime")}</dt><dd>{seconds(typeof runtime.queued_seconds === "number" || typeof runtime.resource_queued_seconds === "number" ? (runtime.queued_seconds ?? 0) + (runtime.resource_queued_seconds ?? 0) : undefined)}</dd>
        <dt>{t("harness.executingTime")}</dt><dd>{seconds(runtime.executing_seconds)}</dd>
        <dt>{t("harness.externalTime")}</dt><dd>{seconds(runtime.external_wait_seconds)}</dd>
        <dt>{t("harness.patch")}</dt><dd>{runtime.has_modification === undefined ? t("common.notRecorded") : runtime.has_modification ? t("harness.patchPresent") : t("harness.patchAbsent")}</dd>
        <dt>{t("harness.lastProgress")}</dt><dd>{runtime.last_effective_progress ?? t("common.notRecorded")}</dd>
        <dt>{t("harness.cache")}</dt><dd>{runtime.cache ? `${runtime.cache.hits} / ${runtime.cache.misses}` : t("common.notRecorded")}</dd>
        <dt>{t("harness.cancel")}</dt><dd>{t(`harness.cancel_${cancellation}`)}</dd>
      </dl>
      {runtime.preparation_progress ? <p className="mt-2">{runtime.preparation_progress.phase}: {JSON.stringify(runtime.preparation_progress.counts)}</p> : null}
      {runtime.last_cache ? <p className="mt-2 break-all text-subtle">{t("harness.cacheReason")}: {runtime.last_cache.reason}</p> : null}
      {runtime.recovery ? <div className="mt-2"><p>{t("harness.reuse")}: {runtime.recovery.reuse}</p><p>{t("harness.redo")}: {runtime.recovery.redo}</p></div> : null}
      {Object.keys(verification).length ? <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t pt-2">
        {["execution", "behavior", "regression", "review", "coverage", "official"].map(key => <div key={key} className="contents"><dt>{t(`harness.${key}`)}</dt><dd className="break-all">{typeof verification[key] === "object" ? JSON.stringify(verification[key]) : verification[key]}</dd></div>)}
      </dl> : null}
      {policy.version ? <p className="mt-2 break-all">{t("harness.policy")}: {policy.version} · {policy.status ?? "pending"} · {policy.sha256}</p> : null}
      {harness.experience_refs?.length ? <p className="mt-1">{t("harness.experience")}: {harness.experience_refs.join(", ")}</p> : null}
      {[["policyConfiguration", runtime.policy_configuration ?? harness.policy_configuration], ["validationLayers", verification.layers], ["contract", harness.contract], ["failure", harness.failure_summary], ["stageTotals", runtime.stage_seconds], ["preparation", harness.preparation], ["environment", harness.environment]].map(([key, value]) => value && Object.keys(value).length ? <details className="mt-2" key={key}><summary>{t(`harness.${key}`)}</summary><pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-all text-[10px]">{JSON.stringify(value, null, 2)}</pre></details> : null)}
      <p className="mt-2 text-subtle">{t("harness.pending")}</p>
    </section>
  );
}
