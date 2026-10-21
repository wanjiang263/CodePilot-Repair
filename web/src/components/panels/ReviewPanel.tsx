import { clsx } from "clsx";
import { AlertTriangle, CheckCircle2, EyeOff, MessageSquareText } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../../hooks/useSessionData";
import { EmptyState } from "../ui/EmptyState";
import { Notice } from "../ui/Notice";

export function ReviewPanel({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const report = session.report;
  if (!report) {
    return (
      <EmptyState
        icon={<MessageSquareText size={16} />}
        title={t("panels.reviewUnavailable")}
        description={t("panels.reviewUnavailableDescription")}
      />
    );
  }
  const review = report.review;
  const unexecuted = review.skip_reason?.length
    ? review.skip_reason
    : report.unexecuted_stages.filter((item) => item.startsWith("review"));

  if (!review.executed) {
    return (
      <div className="space-y-3 p-3">
        <Notice tone="warning">{t("panels.reviewerNotExecuted")}</Notice>
        <div className="rounded-lg border bg-surface px-3 py-2">
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
            <EyeOff size={12} /> {t("panels.whyNotExecuted")}
          </div>
          {unexecuted.length ? (
            <ul className="space-y-1 text-[11.5px] leading-relaxed">
              {unexecuted.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <div className="text-[11.5px] text-subtle">{t("panels.noReason")}</div>
          )}
        </div>
        <div className="text-[10.5px] leading-relaxed text-subtle">
          {t("panels.officialIndependentReview")}
        </div>
      </div>
    );
  }

  return (
    <div className="p-3">
      <div className="mb-3 flex items-center gap-2">
        <span
          className={clsx(
            "inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] font-medium",
            review.approved
              ? "border-success/25 bg-success/10 text-success"
              : "border-warning/25 bg-warning/10 text-warning",
          )}
        >
          {review.approved ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
          {review.approved ? t("panels.approved") : t("panels.notApproved")}
        </span>
        <span className="text-[10.5px] text-subtle">
          {t("panels.highIssues")}: {review.high_issues ?? 0}
        </span>
      </div>

      {review.summary ? (
        <p className="whitespace-pre-wrap break-words rounded-lg border bg-surface px-3 py-2 text-[12px] leading-relaxed">
          {review.summary}
        </p>
      ) : null}

      {review.issues?.length ? (
        <div className="mt-3 space-y-2">
          {review.issues.map((issue, index) => (
            <div key={`${issue.file}-${index}`} className="rounded-lg border bg-surface px-3 py-2">
              <div className="flex items-center gap-1.5">
                <span
                  className={clsx(
                    "rounded px-1.5 py-0.5 text-[9.5px] font-semibold uppercase",
                    issue.severity === "high"
                      ? "bg-danger/10 text-danger"
                      : issue.severity === "medium"
                        ? "bg-warning/10 text-warning"
                        : "bg-elevated text-muted",
                  )}
                >
                  {issue.severity}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-[10.5px]">
                  {issue.file}
                  {issue.line ? `:${issue.line}` : ""}
                </span>
              </div>
              <p className="mt-1 text-[11.5px] leading-relaxed text-muted">{issue.description}</p>
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-3 text-[11.5px] text-subtle">{t("panels.noReviewIssues")}</div>
      )}

      <div className="mt-4 space-y-1.5 border-t pt-3 text-[10.5px] text-muted">
        <div className="flex justify-between gap-2">
          <span>{t("panels.reviewedPatch")}</span>
          <span className="font-mono">{review.reviewed_patch_hash?.slice(0, 16) ?? t("common.notRecorded")}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span>{t("panels.boundToCurrent")}</span>
          <span>{review.bound_to_candidate === null || review.bound_to_candidate === undefined ? t("common.notRecorded") : String(review.bound_to_candidate)}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span>{t("panels.reviewCalls")}</span>
          <span>{review.review_calls ?? t("common.notRecorded")}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span>{t("panels.postReviewRepair")}</span>
          <span>{review.post_review_repair === undefined ? t("common.notRecorded") : String(review.post_review_repair)}</span>
        </div>
      </div>
      <p className="mt-3 text-[10.5px] leading-relaxed text-subtle">
        {t("panels.reviewerScope")}
      </p>
    </div>
  );
}
