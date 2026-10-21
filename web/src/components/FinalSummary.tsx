import { ArrowUpRight, CheckCircle2, CircleSlash, MinusCircle, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../hooks/useSessionData";
import { duration, formatCost, formatNumber } from "../lib/format";
import { isBudgetStop, stopReasonKey, verificationKey } from "../lib/status";
import { Button } from "./ui/Button";
import { StatusBadge } from "./ui/StatusBadge";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[150px_1fr] gap-3 border-b py-2 text-[12px] last:border-b-0">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 leading-relaxed">{children}</span>
    </div>
  );
}

function OutcomeIcon({ state }: { state: "ok" | "warn" | "bad" | "neutral" }) {
  if (state === "ok") return <CheckCircle2 size={13} className="text-success" />;
  if (state === "warn") return <ShieldAlert size={13} className="text-warning" />;
  if (state === "bad") return <CircleSlash size={13} className="text-danger" />;
  return <MinusCircle size={13} className="text-subtle" />;
}

export function FinalSummary({
  session,
  onOpenPanel,
}: {
  session: SessionData;
  onOpenPanel: (tab: "changes" | "files" | "tests" | "review" | "run") => void;
}) {
  const { t } = useTranslation();
  const report = session.report;
  const closure = report?.internal.closure ?? {};
  const verification = session.verificationLevel;
  const verified = verification === "m2_verified" || verification === "sampled_verified";
  const reviewState = report?.review.executed
    ? report.review.approved
      ? t("panels.approved")
      : t("panels.notApproved")
    : t("common.notExecuted");
  const regression = report?.tests.regression;
  const regressionExecuted = Boolean(regression && regression.status !== "NOT EXECUTED");
  const targetStatus = report?.tests.target_tests_status;
  const budgetStop = isBudgetStop(session.stopReason);
  const reasonKey = stopReasonKey(session.stopReason);

  return (
    <section className="mt-8 border-t pt-6" aria-label={t("final.finalOutcome")}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-subtle">
            {t("final.finalOutcome")}
          </div>
          <h2 className="mt-1 text-[17px] font-semibold tracking-[-0.02em]">
            {verified
              ? t("final.verified")
              : session.status === "cancelled"
                ? t("final.cancelled")
                : budgetStop
                  ? t("final.budget")
                  : t("final.unverified")}
          </h2>
        </div>
        <StatusBadge status={session.status} stopReason={session.stopReason} />
      </div>

      <div className="panel px-4 py-2">
        <Row label={t("final.internal")}>
          <span className="inline-flex items-center gap-1.5 font-medium">
            <OutcomeIcon state={verified ? "ok" : verification ? "warn" : "neutral"} />
            {t(verificationKey(verification))}
          </span>
          <span className="ml-2 text-subtle">{t("final.internalHint")}</span>
        </Row>
        <Row label={t("final.official")}>
          {session.official?.recorded ? (
            <span className="inline-flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11.5px]">{session.official.verdict ?? t("common.unknown")}</span>
              <span className="rounded border px-1.5 py-0.5 text-[10px] text-muted">
                {session.official.harness_status ?? "harness"}
              </span>
              <span className="text-subtle">{t("final.officialAuthoritative")}</span>
            </span>
          ) : (
            <span className="text-subtle">{t("final.officialNotRecorded")}</span>
          )}
        </Row>
        <Row label={t("final.stopReason")}>
          {session.stopReason ? (
            <span>
              {reasonKey ? t(reasonKey) : session.stopReason}
              {reasonKey ? (
                <span className="ml-2 font-mono text-[11px] text-subtle">{session.stopReason}</span>
              ) : null}
            </span>
          ) : (
            <span className="text-subtle">{t("final.none")}</span>
          )}
        </Row>
        <Row label={t("final.targetTests")}>
          <span className={targetStatus === "PASS" ? "text-success" : targetStatus ? "text-danger" : "text-subtle"}>
            {targetStatus ?? t("common.notExecuted")}
          </span>
        </Row>
        <Row label={t("final.regression")}>
          {regression && regressionExecuted ? (
            <span>
              {regression.complete ? t("final.complete") : t("final.incomplete")} ·{" "}
              {(regression.levels ?? []).length ? regression.levels?.join(" / ") : t("final.noLevels")} ·{" "}
              {t("final.newFailures")} {regression.new_failures ?? "—"}
            </span>
          ) : (
            <span className="text-subtle">{t("common.notExecuted")}</span>
          )}
        </Row>
        <Row label={t("final.review")}>
          <span className={reviewState === t("panels.approved") ? "text-success" : reviewState === t("common.notExecuted") ? "text-subtle" : "text-warning"}>
            {reviewState}
          </span>
          {report?.review.bound_to_candidate ? (
            <span className="ml-2 text-subtle">{t("final.bound")}</span>
          ) : null}
        </Row>
        <Row label={t("final.iterations")}>
          {session.attempts} / {report?.tests.test_runs ?? "—"}
        </Row>
        <Row label={t("final.runtimeCost")}>
          {duration(session.durationSeconds)} ·{" "}
          {formatCost(session.usage.cost, session.usage.currency)}{" "}
          <span className="text-subtle">
            ({formatNumber(session.usage.input_tokens)} in / {formatNumber(session.usage.output_tokens)} out)
          </span>
        </Row>
        {closure.unexecuted_stages?.length ? (
          <Row label={t("final.notExecuted")}>
            <ul className="space-y-0.5 text-[11.5px] text-muted">
              {closure.unexecuted_stages.map((item: string) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </Row>
        ) : null}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {session.patch ? (
          <Button size="sm" onClick={() => onOpenPanel("changes")} icon={<ArrowUpRight size={13} />}>
            {t("final.reviewChanges")}
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={() => onOpenPanel("tests")}>
          {t("final.testsVerification")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onOpenPanel("run")}>
          {t("final.budgetArtifacts")}
        </Button>
      </div>
    </section>
  );
}
