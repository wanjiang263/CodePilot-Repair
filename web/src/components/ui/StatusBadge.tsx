import { clsx } from "clsx";
import { useTranslation } from "react-i18next";

import { statusMeta, stopReasonKey, type Tone } from "../../lib/status";

const TONE: Record<Tone, string> = {
  neutral: "border-line bg-elevated text-muted",
  accent: "border-accent/25 bg-accent-soft text-accent",
  success: "border-success/25 bg-success/10 text-success",
  warning: "border-warning/25 bg-warning/10 text-warning",
  danger: "border-danger/25 bg-danger/10 text-danger",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-subtle",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

export function StatusBadge({
  status,
  stopReason,
  pulse = false,
  compact = false,
}: {
  status: string;
  stopReason?: string | null;
  pulse?: boolean;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const meta = statusMeta(status);
  const label = t(meta.labelKey);
  const description = t(meta.descriptionKey);
  const reasonKey = stopReasonKey(stopReason);
  const reason = reasonKey ? t(reasonKey) : stopReason ?? null;
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border font-medium",
        compact ? "px-2 py-0.5 text-[10.5px]" : "px-2.5 py-1 text-[11.5px]",
        TONE[meta.tone],
      )}
      title={reason ? `${description} · ${reason}` : description}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          DOT[meta.tone],
          pulse && (status === "running" || status === "queued") && "animate-soft-pulse",
        )}
        aria-hidden
      />
      {label}
      {!compact && reason && reason.toLowerCase() !== label.toLowerCase() ? (
        <span className="opacity-70">· {reason}</span>
      ) : null}
    </span>
  );
}
