import i18n from "../i18n";

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(i18n.language, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return i18n.t("time.justNow");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return i18n.t("time.minutesAgo", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return i18n.t("time.hoursAgo", { count: hours });
  const days = Math.round(hours / 24);
  if (days < 14) return i18n.t("time.daysAgo", { count: days });
  return formatDate(value);
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toLocaleString(i18n.language, { maximumFractionDigits: 1 })}M`;
  if (Math.abs(value) >= 10_000) return `${(value / 1000).toLocaleString(i18n.language, { maximumFractionDigits: 1 })}k`;
  return value.toLocaleString(i18n.language);
}

export function formatCost(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(i18n.language, {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: 2,
      maximumFractionDigits: Math.abs(value) < 1 ? 4 : 2,
    }).format(value);
  } catch {
    return `${value.toFixed(Math.abs(value) < 1 ? 4 : 2)} ${currency}`;
  }
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) {
    return i18n.t("time.seconds", { value: seconds.toFixed(seconds < 10 ? 1 : 0) });
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return i18n.t("time.minutesSeconds", { minutes, seconds: rest });
}

export function shortSha(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length <= length ? value : value.slice(0, length);
}

export function titleFromIssue(issue: string): string {
  const compact = issue.replace(/\s+/g, " ").trim();
  if (!compact) return i18n.t("time.untitledRepair");
  return compact.length > 96 ? `${compact.slice(0, 95)}…` : compact;
}

export function percent(value: number | null | undefined, total: number | null | undefined): number | null {
  if (value === null || value === undefined || !total || total <= 0) return null;
  return Math.min(100, Math.max(0, (value / total) * 100));
}
