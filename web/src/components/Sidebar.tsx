import { clsx } from "clsx";
import {
  Archive,
  ChevronsLeft,
  ChevronsRight,
  FlaskConical,
  History,
  Moon,
  PanelLeft,
  Plus,
  Search,
  Settings,
  Sun,
} from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { HistoryItem, Task } from "../api/types";
import type { ResolvedTheme } from "../hooks/useTheme";
import { relativeTime, titleFromIssue } from "../lib/format";
import type { View } from "../lib/navigation";
import { statusMeta, verificationKey } from "../lib/status";

const DOT: Record<string, string> = {
  neutral: "bg-subtle",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

function Logo({ compact }: { compact: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <div className="relative flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] border border-accent/20 bg-accent-soft text-accent">
        <svg width="17" height="17" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path d="M5 4.5v11M15 4.5v11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          <circle cx="5" cy="4.5" r="1.7" fill="currentColor" />
          <circle cx="15" cy="15.5" r="1.7" fill="currentColor" />
          <path d="M5 10h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </div>
      {!compact ? (
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[13.5px] font-semibold tracking-tight">CodePilot</div>
          <div className="text-[10.5px] uppercase tracking-[0.14em] text-subtle">Repair</div>
        </div>
      ) : null}
    </div>
  );
}

export function Sidebar({
  view,
  onNavigate,
  tasks,
  history,
  collapsed,
  onToggle,
  theme,
  onToggleTheme,
  onSearch,
  connected,
}: {
  view: View;
  onNavigate: (view: View) => void;
  tasks: Task[];
  history: HistoryItem[];
  collapsed: boolean;
  onToggle: () => void;
  theme: ResolvedTheme;
  onToggleTheme: () => void;
  onSearch: () => void;
  connected: boolean;
}) {
  const { t } = useTranslation();
  const activeTask = view.name === "session" ? view.taskId : null;
  const activeBench = view.name === "replay" || view.name === "benchmark" ? view.benchId : null;

  const nav = (name: "history" | "benchmarks" | "settings", icon: ReactNode, label: string) => (
    <button
      key={name}
      className="nav-item w-full"
      data-active={view.name === name}
      onClick={() => onNavigate({ name } as View)}
      title={collapsed ? label : undefined}
      aria-current={view.name === name ? "page" : undefined}
    >
      {icon}
      {!collapsed ? <span className="truncate">{label}</span> : null}
    </button>
  );

  return (
    <aside
      className={clsx(
        "flex h-full shrink-0 flex-col border-r bg-canvas/92 transition-[width] duration-200",
        collapsed ? "w-[64px]" : "w-[264px]",
      )}
      aria-label={t("nav.aria")}
    >
      <div className={clsx("flex items-center px-3 pb-3 pt-4", collapsed ? "justify-center" : "justify-between")}>
        <button onClick={() => onNavigate({ name: "new" })} aria-label={t("nav.home")}>
          <Logo compact={collapsed} />
        </button>
        {!collapsed ? (
          <button className="icon-btn" onClick={onToggle} title={t("nav.collapse")} aria-label={t("nav.collapse")}>
            <ChevronsLeft size={15} />
          </button>
        ) : null}
      </div>

      <div className={clsx("px-3 pb-2", collapsed && "px-2")}>
        <button
          className={clsx("btn btn-primary w-full", collapsed && "px-0")}
          onClick={() => onNavigate({ name: "new" })}
          title={collapsed ? t("nav.newRepair") : undefined}
        >
          <Plus size={15} />
          {!collapsed ? t("nav.newRepair") : null}
        </button>
      </div>

      <div className={clsx("space-y-0.5 px-2 pt-2", collapsed && "px-1.5")}>
        {collapsed ? (
          <button
            className="icon-btn mx-auto mb-1"
            onClick={onToggle}
            title={t("nav.expand")}
            aria-label={t("nav.expand")}
          >
            <PanelLeft size={16} />
          </button>
        ) : null}
        <button
          className={clsx("nav-item w-full", collapsed && "justify-center px-0")}
          onClick={onSearch}
          title={collapsed ? t("nav.search") : undefined}
        >
          <Search size={15} />
          {!collapsed ? (
            <>
              <span className="flex-1 text-left">{t("nav.search")}</span>
              <span className="kbd">Ctrl K</span>
            </>
          ) : null}
        </button>
        {nav("history", <History size={15} />, t("nav.history"))}
        {nav("benchmarks", <FlaskConical size={15} />, t("nav.benchmarks"))}
      </div>

      {!collapsed ? (
        <div className="mt-5 min-h-0 flex-1 overflow-y-auto scroll-thin px-2 pb-3">
          <div className="flex items-center justify-between px-2 pb-1.5">
            <span className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
              {t("nav.recentRepairs")}
            </span>
            {tasks.length > 0 ? <span className="text-[10.5px] text-subtle">{tasks.length}</span> : null}
          </div>
          <div className="space-y-0.5">
            {tasks.slice(0, 9).map((task) => {
              const meta = statusMeta(task.ui_status);
              return (
                <button
                  key={task.task_id}
                  onClick={() => onNavigate({ name: "session", taskId: task.task_id })}
                  className={clsx(
                    "group flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left transition-colors hover:bg-elevated",
                    activeTask === task.task_id && "bg-accent-soft",
                  )}
                  aria-current={activeTask === task.task_id ? "page" : undefined}
                >
                  <span
                    className={clsx(
                      "mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full",
                      DOT[meta.tone],
                      (task.ui_status === "running" || task.ui_status === "queued") && "animate-soft-pulse",
                    )}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px] leading-5 text-ink">
                      {titleFromIssue(task.issue_preview)}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-subtle">
                      <span className="truncate">{task.repository}</span>
                      <span>·</span>
                      <span className="shrink-0">{relativeTime(task.created_at)}</span>
                    </span>
                  </span>
                </button>
              );
            })}
            {tasks.length === 0 ? (
              <div className="px-2 py-3 text-[11.5px] text-subtle">{t("nav.noRuns")}</div>
            ) : null}
          </div>

          {history.length > 0 ? (
            <>
              <div className="flex items-center justify-between px-2 pb-1.5 pt-5">
                <span className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-subtle">
                  {t("nav.replayArchive")}
                </span>
                <Archive size={12} className="text-subtle" />
              </div>
              <div className="space-y-0.5">
                {history.slice(0, 4).map((item) => (
                  <button
                    key={item.history_id}
                    onClick={() => onNavigate({ name: "replay", benchId: item.history_id })}
                    className={clsx(
                      "flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left transition-colors hover:bg-elevated",
                      activeBench === item.history_id && "bg-accent-soft",
                    )}
                  >
                    <span className="mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full bg-subtle" aria-hidden />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12px] leading-5">{item.label}</span>
                      <span className="mt-0.5 block truncate text-[10.5px] text-subtle">
                        {t("history.official", { value: item.official_verdict ?? t("common.notRecorded") })} ·{" "}
                        {t("history.internal", { value: t(verificationKey(item.verification_level)) })}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </div>
      ) : (
        <div className="flex-1" />
      )}

      <div className={clsx("border-t px-2 py-2", collapsed && "px-1.5")}>
        {!collapsed ? (
          <div className="mb-1 flex items-center gap-2 rounded-lg px-2 py-1.5 text-[10.5px] text-muted">
            <span
              className={clsx("h-1.5 w-1.5 rounded-full", connected ? "bg-success" : "bg-warning")}
              aria-hidden
            />
            {connected ? t("nav.connected") : t("nav.connectionIssue")}
          </div>
        ) : null}
        <div className={clsx("flex items-center gap-1", collapsed && "flex-col")}>
          <button
            className="icon-btn"
            onClick={onToggleTheme}
            title={theme === "dark" ? t("app.lightTheme") : t("app.darkTheme")}
            aria-label={theme === "dark" ? t("app.lightTheme") : t("app.darkTheme")}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </button>
          <button
            className="icon-btn"
            onClick={() => onNavigate({ name: "settings" })}
            title={t("nav.settings")}
            aria-label={t("nav.settings")}
          >
            <Settings size={15} />
          </button>
          {!collapsed ? (
            <span className="ml-auto pr-1 text-[10px] text-subtle">Ctrl K</span>
          ) : null}
        </div>
      </div>
      {collapsed ? (
        <button
          className="mb-3 flex justify-center text-subtle hover:text-ink"
          onClick={onToggle}
          aria-label={t("nav.expand")}
        >
          <ChevronsRight size={14} />
        </button>
      ) : null}
    </aside>
  );
}
