import { clsx } from "clsx";
import {
  FileCode2,
  FileText,
  Files,
  FlaskConical,
  Gauge,
  MessageSquareText,
  PanelRightClose,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../hooks/useSessionData";
import { ChangesPanel } from "./panels/ChangesPanel";
import { FilesPanel } from "./panels/FilesPanel";
import { ReportPanel } from "./panels/ReportPanel";
import { ReviewPanel } from "./panels/ReviewPanel";
import { RunPanel } from "./panels/RunPanel";
import { TestsPanel } from "./panels/TestsPanel";

export type DetailTab = "changes" | "files" | "tests" | "review" | "run" | "report";

const TABS: Array<{ id: DetailTab; labelKey: string; icon: typeof FileCode2 }> = [
  { id: "changes", labelKey: "session.tabs.changes", icon: FileCode2 },
  { id: "files", labelKey: "session.tabs.files", icon: Files },
  { id: "tests", labelKey: "session.tabs.tests", icon: FlaskConical },
  { id: "review", labelKey: "session.tabs.review", icon: MessageSquareText },
  { id: "run", labelKey: "session.tabs.run", icon: Gauge },
  { id: "report", labelKey: "session.tabs.report", icon: FileText },
];

export function DetailPanel({
  session,
  open,
  tab,
  onTab,
  width,
  onWidth,
  onClose,
  overlay,
}: {
  session: SessionData;
  open: boolean;
  tab: DetailTab;
  onTab: (tab: DetailTab) => void;
  width: number;
  onWidth: (width: number) => void;
  onClose: () => void;
  overlay: boolean;
}) {
  const { t } = useTranslation();
  const [focusFile, setFocusFile] = useState<string | null>(null);
  const [lastRunId, setLastRunId] = useState(session.runId);
  const resizing = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(width);

  // Drop the focused file synchronously during render when the run changes
  // (the sanctioned "adjust state when a prop changes" pattern).
  if (lastRunId !== session.runId) {
    setLastRunId(session.runId);
    setFocusFile(null);
  }

  useEffect(() => {
    const move = (event: PointerEvent) => {
      if (!resizing.current) return;
      const next = Math.min(760, Math.max(320, startWidth.current + (startX.current - event.clientX)));
      onWidth(next);
    };
    const up = () => {
      resizing.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [onWidth]);

  if (!open) return null;

  const counts: Partial<Record<DetailTab, number>> = {
    changes: session.patch?.files_count ?? 0,
    files: session.patch?.files_count ?? 0,
    review: session.report?.review.issues?.length ?? 0,
  };

  const panel = (
    <aside
      className={clsx(
        "relative flex h-full min-h-0 flex-col border-l bg-canvas",
        overlay ? "absolute inset-y-0 right-0 z-30 w-[min(92vw,560px)] shadow-float" : "shrink-0",
      )}
      style={overlay ? undefined : { width }}
      aria-label={t("session.detailsAria")}
    >
      {!overlay ? (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={t("session.resizePanel")}
          className="absolute -left-1 top-0 z-20 h-full w-2 cursor-col-resize hover:bg-accent/10"
          onPointerDown={(event) => {
            resizing.current = true;
            startX.current = event.clientX;
            startWidth.current = width;
            document.body.style.cursor = "col-resize";
            document.body.style.userSelect = "none";
          }}
        />
      ) : null}
      <div className="flex items-center gap-1 border-b px-2 py-1.5">
        <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto scroll-thin">
          {TABS.map((item) => {
            const Icon = item.icon;
            const count = counts[item.id];
            return (
              <button
                key={item.id}
                className="tab shrink-0"
                data-active={tab === item.id}
                onClick={() => onTab(item.id)}
                role="tab"
                aria-selected={tab === item.id}
                aria-controls={`panel-${item.id}`}
              >
                <Icon size={13} />
                {t(item.labelKey)}
                {count ? <span className="text-[9.5px] text-subtle">{count}</span> : null}
              </button>
            );
          })}
        </div>
        <button className="icon-btn" onClick={onClose} aria-label={t("session.hideDetails")}>
          <PanelRightClose size={15} />
        </button>
      </div>
      <div
        id={`panel-${tab}`}
        role="tabpanel"
        className="min-h-0 flex-1 overflow-y-auto scroll-thin"
      >
        {tab === "changes" ? (
          <ChangesPanel session={session} width={width} focusFile={focusFile} />
        ) : null}
        {tab === "files" ? (
          <FilesPanel
            session={session}
            onOpenFile={(path) => {
              setFocusFile(path);
              onTab("changes");
            }}
          />
        ) : null}
        {tab === "tests" ? <TestsPanel session={session} /> : null}
        {tab === "review" ? <ReviewPanel session={session} /> : null}
        {tab === "run" ? <RunPanel session={session} /> : null}
        {tab === "report" ? <ReportPanel session={session} /> : null}
      </div>
    </aside>
  );

  if (!overlay) return panel;
  return (
    <>
      <div className="absolute inset-0 z-20 overlay-scrim" onClick={onClose} aria-hidden />
      {panel}
    </>
  );
}
