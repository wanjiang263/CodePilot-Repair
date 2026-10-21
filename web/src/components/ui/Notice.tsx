import { AlertTriangle, CheckCircle2, Info, WifiOff } from "lucide-react";
import type { ReactNode } from "react";

export function Notice({
  tone = "info",
  children,
  action,
}: {
  tone?: "info" | "success" | "warning" | "danger";
  children: ReactNode;
  action?: ReactNode;
}) {
  const icon =
    tone === "success" ? (
      <CheckCircle2 size={14} />
    ) : tone === "warning" ? (
      <AlertTriangle size={14} />
    ) : tone === "danger" ? (
      <WifiOff size={14} />
    ) : (
      <Info size={14} />
    );
  const classes = {
    info: "border-line bg-elevated text-muted",
    success: "border-success/25 bg-success/10 text-success",
    warning: "border-warning/25 bg-warning/10 text-warning",
    danger: "border-danger/25 bg-danger/10 text-danger",
  }[tone];
  return (
    <div className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-[12px] ${classes}`}>
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1 leading-relaxed">{children}</div>
      {action}
    </div>
  );
}
