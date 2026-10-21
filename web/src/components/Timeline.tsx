import {
  ArrowDown,
  CheckCircle2,
  ChevronRight,
  Circle,
  CircleAlert,
  CircleSlash,
  Loader2,
  Terminal,
  Wrench,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import type { EventRecord } from "../api/types";
import type { SessionData } from "../hooks/useSessionData";
import { formatCost, formatDate, formatNumber } from "../lib/format";
import { isTerminal } from "../lib/status";
import {
  eventLabel,
  groupEvents,
  groupTone,
  visibleEvents,
  visibleGroups,
  type Density,
  type NodeGroup,
} from "../lib/stages";
import { FinalSummary } from "./FinalSummary";
import { EmptyState } from "./ui/EmptyState";

function toneIcon(group: NodeGroup) {
  const tone = groupTone(group);
  if (tone === "running") return <Loader2 size={12} className="animate-spin text-accent" />;
  if (tone === "ok") return <CheckCircle2 size={12} className="text-success" />;
  if (tone === "bad") return <CircleAlert size={12} className="text-danger" />;
  if (tone === "warn") return <CircleSlash size={12} className="text-warning" />;
  return <Circle size={10} className="text-subtle" />;
}

function statusLabel(status: string, t: TFunction): string {
  if (status.startsWith("budget_")) return t("stages.statuses.budget");
  if (status.startsWith("skipped")) return t("stages.statuses.skipped");
  return t(`stages.statuses.${status}`, { defaultValue: status.replaceAll("_", " ") });
}

function translate(t: TFunction, key: string): string {
  return key.startsWith("stages.") ? t(key) : key;
}

function UsageLine({ event }: { event: EventRecord }) {
  const { t } = useTranslation();
  if (!event.usage) return null;
  return (
    <span className="text-[10.5px] text-subtle">
      {event.usage.model} · {formatNumber(event.usage.input_tokens)} in /{" "}
      {formatNumber(event.usage.output_tokens)} out ·{" "}
      {formatCost(event.usage.cost, event.usage.currency)}
      {!event.usage.cost_known ? ` · ${t("stages.costUnknown")}` : ""}
    </span>
  );
}

function ToolCallList({ calls }: { calls: Array<Record<string, unknown>> }) {
  const { t } = useTranslation();
  if (!calls.length) return null;
  return (
    <div className="mt-2 space-y-1">
      {calls.map((call, index) => {
        const name = String(call.name ?? call.tool ?? "tool");
        const ok = call.ok;
        return (
          <details key={`${name}-${index}`} className="rounded-md border bg-canvas/70 px-2.5 py-1.5">
            <summary className="flex cursor-pointer list-none items-center gap-2 text-[11.5px]">
              <Wrench size={11} className="text-muted" />
              <span className="font-mono">{name}</span>
              {ok === false ? <span className="text-danger">{t("stages.toolFailed")}</span> : null}
              {ok === true ? <span className="text-success">{t("stages.toolOk")}</span> : null}
            </summary>
            <pre className="code-surface mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[10.5px] text-muted">
              {JSON.stringify(call.arguments ?? call, null, 2)}
            </pre>
          </details>
        );
      })}
    </div>
  );
}

function EventLine({ event }: { event: EventRecord }) {
  const { t } = useTranslation();
  const error = event.error;
  return (
    <div className="rounded-md px-2 py-1.5 hover:bg-elevated/70">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
        <span className="font-medium text-muted">{translate(t, eventLabel(event))}</span>
        {event.status ? <span className="text-[10px] text-subtle">{statusLabel(event.status, t)}</span> : null}
        {event.summary ? <span className="min-w-0 flex-1 truncate">{event.summary}</span> : null}
        <UsageLine event={event} />
      </div>
      {error ? <div className="mt-1 text-[11px] leading-relaxed text-danger">{error}</div> : null}
      {event.tool_calls?.length ? <ToolCallList calls={event.tool_calls} /> : null}
    </div>
  );
}

function Group({
  group,
  density,
  onOpenDetails,
}: {
  group: NodeGroup;
  density: Density;
  onOpenDetails: () => void;
}) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = useState(false);
  const baseEvents = visibleEvents(group, density);
  const events = showAll ? group.events : baseEvents;
  const hidden = group.events.length - baseEvents.length;
  const running = group.status === "started" || group.status === "running";
  return (
    <div className="relative flex gap-3 pb-4">
      <div className="relative flex w-5 shrink-0 flex-col items-center">
        <div className="z-10 mt-1 flex h-5 w-5 items-center justify-center rounded-full border bg-surface">
          {toneIcon(group)}
        </div>
        <div className="absolute bottom-0 top-6 w-px bg-line" aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[12.5px] font-semibold tracking-[-0.01em]">{translate(t, group.label)}</span>
          <span className="text-[10.5px] text-subtle">{statusLabel(group.status, t)}</span>
          {group.durationMs !== null ? (
            <span className="text-[10.5px] text-subtle">
              {group.durationMs < 1000
                ? `${group.durationMs} ms`
                : `${(group.durationMs / 1000).toFixed(1)} s`}
            </span>
          ) : null}
          {running ? <span className="text-[10.5px] text-accent">{t("stages.inProgress")}</span> : null}
        </div>
        {group.summary ? (
          <div className="mt-1 whitespace-pre-wrap break-words text-[12px] leading-relaxed text-muted">
            {group.summary}
          </div>
        ) : null}
        {group.error ? (
          <div className="mt-1 whitespace-pre-wrap break-words text-[11.5px] leading-relaxed text-danger">
            {group.error}
          </div>
        ) : null}
        {events.length ? (
          <div className="mt-2 space-y-0.5 border-l pl-2">
            {events.map((event) => (
              <EventLine key={event.seq} event={event} />
            ))}
          </div>
        ) : null}
        {hidden > 0 ? (
          <button
            className="mt-1.5 inline-flex items-center gap-1 text-[10.5px] text-subtle hover:text-ink"
            onClick={() => {
              setShowAll((value) => !value);
              if (!showAll) onOpenDetails();
            }}
          >
            <ChevronRight size={11} className={showAll ? "rotate-90" : ""} />
            {showAll ? t("stages.hideLowLevel") : t("stages.lowLevel", { count: hidden })}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function PlanSummary({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const plan = session.report?.plan as
    | {
        root_cause_hypothesis?: string;
        changes?: Array<{ file?: string; action?: string; description?: string }>;
        tests_to_run?: string[];
      }
    | undefined;
  const localization = session.report?.localization as
    | { files?: Array<{ path?: string; reason?: string }> }
    | undefined;
  if (!plan?.root_cause_hypothesis && !localization?.files?.length) return null;
  return (
    <div className="mb-5 rounded-xl border bg-surface px-4 py-3">
      <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-[0.13em] text-subtle">
        {t("stages.agentReading")}
      </div>
      {plan?.root_cause_hypothesis ? (
        <p className="text-[12.5px] leading-relaxed">{plan.root_cause_hypothesis}</p>
      ) : null}
      {plan?.changes?.length ? (
        <ul className="mt-2 space-y-1 text-[11.5px] text-muted">
          {plan.changes.slice(0, 6).map((change, index) => (
            <li key={`${change.file}-${index}`}>
              <span className="font-mono text-[10.5px]">{change.file}</span>
              {change.action ? <span className="ml-1.5 text-subtle">{change.action}</span> : null}
              {change.description ? <span> — {change.description}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {localization?.files?.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {localization.files.slice(0, 5).map((file) => (
            <span key={file.path} className="rounded border bg-canvas px-1.5 py-0.5 font-mono text-[10px] text-muted">
              {file.path}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function Timeline({
  session,
  density,
  onOpenPanel,
}: {
  session: SessionData;
  density: Density;
  onOpenPanel: (tab: "changes" | "files" | "tests" | "review" | "run") => void;
}) {
  const { t } = useTranslation();
  const groups = useMemo(() => groupEvents(session.events), [session.events]);
  const visible = useMemo(() => visibleGroups(groups, density), [groups, density]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const terminal = isTerminal(session.status);

  useEffect(() => {
    setPinned(true);
  }, [session.runId]);

  useEffect(() => {
    if (terminal) setPinned(true);
  }, [terminal]);

  useEffect(() => {
    if (!pinned) return;
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [session.events.length, pinned]);

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    setPinned(distance < 72);
  };

  const jump = () => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
    setPinned(true);
  };

  return (
    <div className="relative flex min-h-0 flex-1 flex-col" data-testid="timeline">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto scroll-thin px-5 py-6"
      >
        <div className="mx-auto w-full max-w-[820px]">
          <div className="mb-6 flex gap-3">
            <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border bg-elevated text-[10px] font-semibold text-muted">
              {t("stages.you")}
            </div>
            <div className="min-w-0 flex-1">
              <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[10.5px] text-subtle">
                <span className="font-medium text-muted">{session.repository}</span>
                <span>·</span>
                <span>{formatDate(session.createdAt)}</span>
                {session.readonly ? <span>· {t("stages.historicalReplay")}</span> : null}
              </div>
              <div className="whitespace-pre-wrap break-words rounded-xl border bg-surface px-4 py-3 text-[13px] leading-relaxed">
                {session.issue || t("stages.issueNotRecorded")}
              </div>
            </div>
          </div>

          <PlanSummary session={session} />

          {session.loading && !session.events.length ? (
            <div className="space-y-3 py-4">
              {[0, 1, 2].map((index) => (
                <div key={index} className="flex gap-3">
                  <div className="skeleton h-5 w-5 rounded-full" />
                  <div className="flex-1 space-y-2">
                    <div className="skeleton h-3 w-32" />
                    <div className="skeleton h-3 w-2/3" />
                  </div>
                </div>
              ))}
            </div>
          ) : null}

          {!session.loading && !session.events.length ? (
            <EmptyState
              icon={<Terminal size={16} />}
              title={t("stages.noEvents")}
              description={t("stages.noEventsDescription")}
            />
          ) : null}

          <div data-testid="timeline-stages">
            {visible.map((group) => (
              <Group
                key={group.id}
                group={group}
                density={density}
                onOpenDetails={() => onOpenPanel("run")}
              />
            ))}
          </div>

          {session.status === "running" || session.status === "queued" ? (
            <div className="flex items-center gap-2 pb-4 ps-8 text-[11px] text-muted">
              <Loader2 size={12} className="animate-spin text-accent" />
              {session.status === "queued" ? t("stages.waiting") : t("stages.working")}
            </div>
          ) : null}

          {terminal ? <FinalSummary session={session} onOpenPanel={onOpenPanel} /> : null}
        </div>
      </div>

      {!pinned && session.events.length > 0 ? (
        <button
          className="absolute bottom-4 right-4 inline-flex items-center gap-1.5 rounded-full border bg-surface px-3 py-1.5 text-[11px] shadow-soft hover:bg-elevated"
          onClick={jump}
        >
          <ArrowDown size={12} /> {t("stages.latestEvents")}
        </button>
      ) : null}
    </div>
  );
}
