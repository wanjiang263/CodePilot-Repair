import { clsx } from "clsx";
import { CornerDownLeft, History, Search, TerminalSquare } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { HistoryItem, Task } from "../api/types";
import { relativeTime, titleFromIssue } from "../lib/format";
import { statusMeta, verificationKey } from "../lib/status";

type Item =
  | { kind: "task"; task: Task }
  | { kind: "history"; history: HistoryItem };

const DOT: Record<string, string> = {
  neutral: "bg-subtle",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
};

export function CommandPalette({
  open,
  tasks,
  history,
  onClose,
  onSelectTask,
  onSelectReplay,
}: {
  open: boolean;
  tasks: Task[];
  history: HistoryItem[];
  onClose: () => void;
  onSelectTask: (taskId: string) => void;
  onSelectReplay: (benchId: string) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 20);
    return () => window.clearTimeout(timer);
  }, [open]);

  const items = useMemo<Item[]>(() => {
    const needle = query.trim().toLowerCase();
    const taskItems = tasks
      .filter((task) =>
        !needle
          ? true
          : `${task.issue_preview} ${task.repository} ${task.task_id}`.toLowerCase().includes(needle),
      )
      .slice(0, 8)
      .map((task): Item => ({ kind: "task", task }));
    const historyItems = history
      .filter((item) =>
        !needle
          ? true
          : `${item.label} ${item.instance_id} ${item.model}`.toLowerCase().includes(needle),
      )
      .slice(0, 5)
      .map((item): Item => ({ kind: "history", history: item }));
    return [...taskItems, ...historyItems];
  }, [history, query, tasks]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  if (!open) return null;

  const choose = (item: Item | undefined) => {
    if (!item) return;
    if (item.kind === "task") onSelectTask(item.task.task_id);
    else onSelectReplay(item.history.history_id);
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overlay-scrim px-4 pt-[12vh] backdrop-blur-[1px] animate-fade-in"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("command.searchTasks")}
        className="w-full max-w-[600px] overflow-hidden rounded-2xl border bg-surface shadow-float animate-slide-up"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setActive((value) => Math.min(items.length - 1, value + 1));
          }
          if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive((value) => Math.max(0, value - 1));
          }
          if (event.key === "Enter") {
            event.preventDefault();
            choose(items[active]);
          }
        }}
      >
        <div className="flex items-center gap-2 border-b px-3 py-2.5">
          <Search size={15} className="text-subtle" />
          <input
            ref={inputRef}
            className="flex-1 bg-transparent text-[13px] outline-none placeholder:text-subtle"
            placeholder={t("command.placeholder")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label={t("command.aria")}
          />
          <span className="kbd">{t("command.esc")}</span>
        </div>
        <div className="max-h-[52vh] overflow-y-auto scroll-thin py-1" role="listbox">
          {!items.length ? (
            <div className="px-4 py-8 text-center text-[12px] text-subtle">{t("command.noMatches")}</div>
          ) : null}
          {items.map((item, index) => {
            const isActive = index === active;
            if (item.kind === "task") {
              const meta = statusMeta(item.task.ui_status);
              return (
                <button
                  key={`t-${item.task.task_id}`}
                  role="option"
                  aria-selected={isActive}
                  className={clsx(
                    "flex w-full items-start gap-2.5 px-3 py-2 text-left",
                    isActive && "bg-accent-soft",
                  )}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => choose(item)}
                >
                  <span className={clsx("mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full", DOT[meta.tone])} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[12.5px]">{titleFromIssue(item.task.issue_preview)}</span>
                    <span className="mt-0.5 block truncate text-[10.5px] text-subtle">
                      {item.task.repository} · {relativeTime(item.task.created_at)}
                    </span>
                  </span>
                  <TerminalSquare size={13} className="mt-1 text-subtle" />
                </button>
              );
            }
            return (
              <button
                key={`h-${item.history.history_id}`}
                role="option"
                aria-selected={isActive}
                className={clsx(
                  "flex w-full items-start gap-2.5 px-3 py-2 text-left",
                  isActive && "bg-accent-soft",
                )}
                onMouseEnter={() => setActive(index)}
                onClick={() => choose(item)}
              >
                <span className="mt-[6px] h-1.5 w-1.5 shrink-0 rounded-full bg-subtle" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12.5px]">{item.history.label}</span>
                  <span className="mt-0.5 block truncate text-[10.5px] text-subtle">
                    replay · {t("command.official")} {item.history.official_verdict ?? t("common.notRecorded")} · {t("command.internal")}{" "}
                    {t(verificationKey(item.history.verification_level))}
                  </span>
                </span>
                <History size={13} className="mt-1 text-subtle" />
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-3 border-t px-3 py-2 text-[10px] text-subtle">
          <span className="inline-flex items-center gap-1">
            <CornerDownLeft size={10} /> {t("command.open")}
          </span>
          <span>{t("command.navigate")}</span>
          <span className="ml-auto">Ctrl K</span>
        </div>
      </div>
    </div>
  );
}
