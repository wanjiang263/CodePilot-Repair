import { clsx } from "clsx";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export function SettingsSection({
  title,
  description,
  scope,
  children,
  actions,
}: {
  title: string;
  description?: string;
  scope?: "instant" | "newTasks" | "restart" | "readOnly";
  children: ReactNode;
  actions?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[15px] font-semibold tracking-[-0.01em]">{title}</h2>
            {scope ? (
              <span className="rounded-full border bg-canvas px-2 py-0.5 text-[9.5px] uppercase tracking-[0.08em] text-subtle">
                {t(`settings.scope.${scope}`)}
              </span>
            ) : null}
          </div>
          {description ? (
            <p className="mt-1 max-w-2xl text-[12px] leading-relaxed text-muted">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Field({
  label,
  hint,
  children,
  wide = false,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <label className={clsx("block", wide && "sm:col-span-2")}>
      <span className="mb-1 block text-[11.5px] font-medium text-muted">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-[10.5px] leading-relaxed text-subtle">{hint}</span> : null}
    </label>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T;
  options: Array<{ value: T; label: string; disabled?: boolean; title?: string }>;
  onChange: (value: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="inline-flex rounded-lg border bg-surface p-0.5" role="group" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className="rounded-md px-2.5 py-1 text-[11.5px] text-muted transition-colors data-[active=true]:bg-elevated data-[active=true]:text-ink disabled:cursor-not-allowed disabled:opacity-60"
          data-active={value === option.value}
          disabled={option.disabled}
          title={option.title}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function StatusPill({
  ok,
  label,
  tone,
}: {
  ok?: boolean;
  label: string;
  tone?: "neutral" | "success" | "warning" | "danger" | "accent";
}) {
  const resolved = tone ?? (ok === undefined ? "neutral" : ok ? "success" : "danger");
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium",
        resolved === "success" && "border-success/25 bg-success/10 text-success",
        resolved === "warning" && "border-warning/25 bg-warning/10 text-warning",
        resolved === "danger" && "border-danger/25 bg-danger/10 text-danger",
        resolved === "accent" && "border-accent/25 bg-accent-soft text-accent",
        resolved === "neutral" && "border-line bg-elevated text-muted",
      )}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          resolved === "success" && "bg-success",
          resolved === "warning" && "bg-warning",
          resolved === "danger" && "bg-danger",
          resolved === "accent" && "bg-accent",
          resolved === "neutral" && "bg-subtle",
        )}
        aria-hidden
      />
      {label}
    </span>
  );
}

export function EmptyCard({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed bg-canvas/40 px-4 py-8 text-center text-[12px] leading-relaxed text-subtle">
      {children}
    </div>
  );
}
