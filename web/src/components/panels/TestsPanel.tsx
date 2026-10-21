import { clsx } from "clsx";
import { AlertTriangle, CheckCircle2, CircleSlash, MinusCircle } from "lucide-react";
import type { ReactNode } from "react";
import { Trans, useTranslation } from "react-i18next";

import type { SessionData } from "../../hooks/useSessionData";
import { isBudgetStop, stopReasonKey, verificationKey } from "../../lib/status";
import { EmptyState } from "../ui/EmptyState";
import { Notice } from "../ui/Notice";

function StatusChip({ value, label }: { value: string | null | undefined; label?: string }) {
  const normalized = (value ?? "NOT_EXECUTED").replaceAll(" ", "_").toUpperCase();
  const tone =
    normalized === "PASS" || normalized === "PASSED" || normalized === "COMPLETE"
      ? "success"
      : normalized === "NOT_EXECUTED" || normalized === "NOT_RECORDED" || normalized === "SKIPPED"
        ? "neutral"
        : normalized.includes("BUDGET")
          ? "warning"
          : "danger";
  const icon =
    tone === "success" ? (
      <CheckCircle2 size={11} />
    ) : tone === "warning" ? (
      <AlertTriangle size={11} />
    ) : tone === "danger" ? (
      <CircleSlash size={11} />
    ) : (
      <MinusCircle size={11} />
    );
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10.5px] font-medium",
        tone === "success" && "border-success/25 bg-success/10 text-success",
        tone === "warning" && "border-warning/25 bg-warning/10 text-warning",
        tone === "danger" && "border-danger/25 bg-danger/10 text-danger",
        tone === "neutral" && "border-line bg-elevated text-subtle",
      )}
    >
      {icon}
      {label ?? (value || "NOT EXECUTED")}
    </span>
  );
}

function Section({
  title,
  children,
  extra,
}: {
  title: string;
  children: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <section className="border-b px-3 py-3 last:border-b-0">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">{title}</h3>
        {extra}
      </div>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[128px_1fr] gap-2 py-1 text-[11.5px]">
      <span className="text-muted">{label}</span>
      <span className="min-w-0 leading-relaxed">{children}</span>
    </div>
  );
}

export function TestsPanel({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const report = session.report;
  if (!report) {
    if (session.status === "queued" || session.status === "running") {
      return (
        <EmptyState
          title={t("panels.verificationRunning")}
          description={t("panels.verificationRunningDescription")}
        />
      );
    }
    return (
      <EmptyState
        title={t("panels.noReport")}
        description={t("panels.noReportDescription")}
      />
    );
  }
  const tests = report.tests;
  const regression = tests.regression ?? {};
  const baseline = tests.baseline ?? {};
  const closure = report.internal.closure ?? {};
  const execution = tests.target_execution as
    | {
        status?: string;
        tests_passed?: number;
        tests_failed?: number;
        error_reason?: string;
        tests?: Array<{ node_id?: string; outcome?: string; message?: string }>;
      }
    | null
    | undefined;
  const budgetStop = isBudgetStop(report.stop_reason);
  const reasonKey = stopReasonKey(report.stop_reason);
  const diagnosisSignatures = report.attempts
    .filter((attempt) => attempt.diagnosis)
    .flatMap((attempt) => attempt.failure_signatures);
  const regressionStatus = regression.status;
  const regressionExecuted = Boolean(regressionStatus && regressionStatus !== "NOT EXECUTED");

  return (
    <div>
      {budgetStop ? (
        <div className="px-3 pt-3">
          <Notice tone="warning">
            <Trans
              i18nKey="panels.budgetStop"
              values={{ reason: reasonKey ? t(reasonKey) : report.stop_reason }}
              components={{ strong: <strong /> }}
            />
          </Notice>
        </div>
      ) : null}

      <Section title={t("panels.verdicts")}>
        <Row label={t("panels.official")}>
          {report.official.recorded ? (
            <span>
              <span className="font-mono">{report.official.verdict ?? t("common.unknown")}</span>
              <span className="ml-2 text-subtle">
                {report.official.harness_status ?? "official harness"}
              </span>
            </span>
          ) : (
            <span className="text-subtle">{t("final.officialNotRecorded")}</span>
          )}
        </Row>
        <Row label={t("panels.internal")}>
          <span className="font-medium">{t(verificationKey(report.verification_level))}</span>
          <span className="ml-2 text-subtle">{t("panels.independentOfficial")}</span>
        </Row>
        <Row label={t("panels.runStatus")}>
          <span className="font-mono text-[11px]">{report.status}</span>
          {report.stop_reason ? (
            <span className="ml-2 text-subtle">{reasonKey ? t(reasonKey) : report.stop_reason}</span>
          ) : null}
        </Row>
        <Row label={t("panels.harnessStatus")}>
          {report.official.recorded ? report.official.harness_status : t("common.notRecorded")}
        </Row>
      </Section>

      <Section title={t("panels.targetTests")} extra={<StatusChip value={tests.target_tests_status} />}>
        <Row label={t("panels.executed")}>{tests.target_tests_executed ? t("common.yes") : t("common.notExecuted")}</Row>
        <Row label={t("panels.status")}>{execution?.status ?? tests.target_tests_status ?? t("common.notExecuted")}</Row>
        <Row label={t("panels.passFail")}>
          {execution
            ? t("panels.passedFailed", {
                passed: execution.tests_passed ?? 0,
                failed: execution.tests_failed ?? 0,
              })
            : t("common.notRecorded")}
        </Row>
        {execution?.error_reason ? <Row label={t("common.error")}>{execution.error_reason}</Row> : null}
        {closure.target_test_sha256 ? (
          <Row label={t("panels.patchIdentity")}>
            <span className="font-mono text-[10.5px]">{String(closure.target_test_sha256).slice(0, 16)}</span>
          </Row>
        ) : null}
        {execution?.tests?.length ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-muted">
              {t("panels.testCases", { count: execution.tests.length })}
            </summary>
            <div className="mt-1.5 max-h-44 space-y-1 overflow-auto scroll-thin rounded border bg-canvas/50 p-2">
              {execution.tests.map((test) => (
                <div key={test.node_id} className="flex gap-2 text-[10.5px]">
                  <span
                    className={
                      test.outcome === "passed"
                        ? "text-success"
                        : test.outcome === "skipped"
                          ? "text-subtle"
                          : "text-danger"
                    }
                  >
                    {test.outcome}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-mono">{test.node_id}</span>
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </Section>

      <Section
        title={t("panels.regression")}
        extra={<StatusChip value={regressionExecuted ? (regression.complete ? "COMPLETE" : "INCOMPLETE") : "NOT EXECUTED"} label={regressionExecuted ? (regression.complete ? t("panels.complete") : t("panels.incomplete")) : t("common.notExecuted")} />}
      >
        <Row label={t("panels.executed")}>{regressionExecuted ? t("common.yes") : t("common.notExecuted")}</Row>
        <Row label={t("panels.complete")}>
          {regressionExecuted ? (regression.complete ? t("common.yes") : t("common.no")) : t("common.notExecuted")}
        </Row>
        <Row label={t("panels.levels")}>
          {regression.levels?.length ? regression.levels.join(" / ") : t("common.notExecuted")}
        </Row>
        <Row label={t("panels.mode")}>{regression.mode ?? t("common.notRecorded")}</Row>
        <Row label={t("panels.newFailures")}>
          {regression.new_failures === null || regression.new_failures === undefined
            ? t("common.notRecorded")
            : regression.new_failures}
        </Row>
        <Row label={t("panels.executedTests")}>{regression.executed_tests ?? t("common.notRecorded")}</Row>
        {regression.failure_reason ? (
          <Row label={t("panels.notCompleteBecause")}>{regression.failure_reason}</Row>
        ) : null}
        <Row label={t("panels.baseline")}>
          {baseline.status === "NOT RECORDED"
            ? t("common.notRecorded")
            : t("panels.baselineValue", {
                count: baseline.test_count ?? 0,
                complete: String(baseline.complete),
                flaky: (baseline.flaky ?? []).length,
              })}
        </Row>
      </Section>

      <Section title={t("panels.reviewDiagnosis")}>
        <Row label={t("panels.reviewerLabel")}>
          {report.review.executed ? (
            <span className={report.review.approved ? "text-success" : "text-warning"}>
              {report.review.approved ? t("panels.approved") : t("panels.notApproved")}
            </span>
          ) : (
            <span className="text-subtle">{t("common.notExecuted")}</span>
          )}
        </Row>
        <Row label={t("panels.boundToCandidate")}>
          {report.review.bound_to_candidate === null || report.review.bound_to_candidate === undefined
            ? t("common.notRecorded")
            : String(report.review.bound_to_candidate)}
        </Row>
        <Row label={t("panels.highIssues")}>{report.review.high_issues ?? "—"}</Row>
        <Row label={t("panels.diagnosis")}>
          {diagnosisSignatures.length ? (
            <ul className="space-y-1">
              {[...new Set(diagnosisSignatures)].slice(0, 6).map((signature) => (
                <li key={signature} className="truncate font-mono text-[10.5px]" title={signature}>
                  {signature}
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-subtle">{t("panels.noFailureSignature")}</span>
          )}
        </Row>
        {report.attempts.length ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] text-muted">
              {t("panels.failureHistory", { count: report.attempts.length })}
            </summary>
            <div className="mt-1.5 overflow-x-auto rounded border bg-canvas/40">
              <table className="w-full text-[10px]">
                <thead>
                  <tr className="border-b text-left text-subtle">
                    <th className="py-1 pr-2 font-medium">{t("panels.tableIteration")}</th>
                    <th className="py-1 pr-2 font-medium">{t("panels.tableTarget")}</th>
                    <th className="py-1 pr-2 font-medium">{t("panels.tableFailed")}</th>
                    <th className="py-1 pr-2 font-medium">{t("panels.tableDiagnosis")}</th>
                    <th className="py-1 font-medium">{t("panels.tableSignatures")}</th>
                  </tr>
                </thead>
                <tbody>
                  {report.attempts.map((attempt) => (
                    <tr key={attempt.iteration} className="border-b last:border-0">
                      <td className="py-1 pr-2 font-mono">{attempt.iteration}</td>
                      <td className="py-1 pr-2">{attempt.target_status ?? t("common.notExecuted")}</td>
                      <td className="py-1 pr-2">{attempt.tests_failed ?? "—"}</td>
                      <td className="py-1 pr-2">{attempt.diagnosis ? t("common.yes") : t("common.no")}</td>
                      <td className="max-w-[180px] truncate py-1 font-mono text-subtle">
                        {attempt.failure_signatures.slice(0, 2).join(", ") || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {report.failures.length ? (
              <ul className="mt-1.5 space-y-0.5 text-[10px] text-subtle">
                {report.failures.slice(0, 5).map((failure) => (
                  <li key={failure} className="truncate" title={failure}>
                    {failure}
                  </li>
                ))}
              </ul>
            ) : null}
          </details>
        ) : null}
      </Section>

      {report.unexecuted_stages.length ? (
        <Section title={t("panels.notExecutedSection")}>
          <ul className="space-y-1 text-[11px] leading-relaxed text-muted">
            {report.unexecuted_stages.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      {closure.closure_admission ? (
        <Section title={t("panels.closureAdmission")}>
          <Row label={t("panels.feasible")}>
            {String((closure.closure_admission as Record<string, unknown>).feasible)}
          </Row>
          <Row label={t("panels.requiredRemaining")}>
            {String((closure.closure_admission as Record<string, unknown>).required)} /{" "}
            {String((closure.closure_admission as Record<string, unknown>).remaining)}
          </Row>
          <Row label={t("panels.codingShare")}>
            {String((closure.closure_admission as Record<string, unknown>).coder_share)}
          </Row>
          <Row label={t("panels.trigger")}>
            {String((closure.closure_admission as Record<string, unknown>).trigger)}
          </Row>
        </Section>
      ) : null}
    </div>
  );
}
