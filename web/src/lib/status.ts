import type { UiStatus } from "../api/types";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger";

const STATUS_META: Record<UiStatus, { labelKey: string; tone: Tone; descriptionKey: string }> = {
  queued: { labelKey: "status.queued.label", tone: "neutral", descriptionKey: "status.queued.description" },
  running: { labelKey: "status.running.label", tone: "accent", descriptionKey: "status.running.description" },
  completed: { labelKey: "status.completed.label", tone: "success", descriptionKey: "status.completed.description" },
  stopped: { labelKey: "status.stopped.label", tone: "warning", descriptionKey: "status.stopped.description" },
  failed: { labelKey: "status.failed.label", tone: "danger", descriptionKey: "status.failed.description" },
  cancelled: { labelKey: "status.cancelled.label", tone: "neutral", descriptionKey: "status.cancelled.description" },
  error: { labelKey: "status.error.label", tone: "danger", descriptionKey: "status.error.description" },
  uncertain: { labelKey: "status.uncertain.label", tone: "warning", descriptionKey: "status.uncertain.description" },
};

export function statusMeta(status: string | null | undefined): {
  labelKey: string;
  tone: Tone;
  descriptionKey: string;
} {
  return STATUS_META[(status ?? "error") as UiStatus] ?? STATUS_META.error;
}

const STOP_REASONS = new Set([
  "budget_iterations",
  "budget_test_runs",
  "budget_tokens",
  "budget_cost",
  "budget_time",
  "budget_tool_calls",
  "budget_review_calls",
  "budget_closure_test_runs",
  "repeated_patch",
  "repeated_failure",
  "no_progress",
  "sandbox_failure",
  "validation_loop",
  "diagnosis_give_up",
  "patch_rejected",
  "tests_failed",
  "tool_error",
  "workflow_error",
  "plan_rejected",
  "cancelled",
]);

export function stopReasonKey(reason: string | null | undefined): string | null {
  if (!reason || !STOP_REASONS.has(reason)) return null;
  return `stopReasons.${reason}`;
}

const VERIFICATION_LEVELS = new Set([
  "m2_verified",
  "sampled_verified",
  "provisionally_verified",
  "unverified",
  "budget_exhausted",
  "cancelled",
  "stopped",
  "failed",
  "error",
]);

export function verificationKey(level: string | null | undefined): string {
  if (!level || !VERIFICATION_LEVELS.has(level)) return level ? level : "verification.notRecorded";
  return `verification.${level}`;
}

export function isTerminal(status: string | null | undefined): boolean {
  return ["completed", "stopped", "failed", "cancelled", "error", "uncertain"].includes(
    status ?? "",
  );
}

export function isBudgetStop(reason: string | null | undefined): boolean {
  return Boolean(reason && reason.startsWith("budget_"));
}
