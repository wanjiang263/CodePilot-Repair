import {
  Check,
  Clipboard,
  PanelLeft,
  PanelRightClose,
  PanelRightOpen,
  ShieldQuestion,
  Square,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../hooks/useSessionData";
import type { Density } from "../lib/stages";
import { formatDate, duration, titleFromIssue } from "../lib/format";
import { isTerminal, stopReasonKey } from "../lib/status";
import { Button } from "./ui/Button";
import { Modal } from "./ui/Modal";
import { Notice } from "./ui/Notice";
import { StatusBadge } from "./ui/StatusBadge";

export function SessionHeader({
  session,
  density,
  onDensity,
  detailOpen,
  onToggleDetail,
  onOpenPanel,
  onToggleSidebar,
}: {
  session: SessionData;
  density: Density;
  onDensity: (density: Density) => void;
  detailOpen: boolean;
  onToggleDetail: () => void;
  onOpenPanel: (tab: "changes" | "files" | "tests" | "review" | "run") => void;
  onToggleSidebar: () => void;
}) {
  const { t } = useTranslation();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const terminal = isTerminal(session.status);
  const canCancel = !session.readonly && !terminal && !session.cancelRequested;
  const title = session.issue
    ? titleFromIssue(session.issue)
    : t("session.runFallback", { runId: session.runId ?? "" });
  const reason = stopReasonKey(session.stopReason);

  const copyRun = async () => {
    if (!session.runId) return;
    try {
      await navigator.clipboard.writeText(session.runId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  const cancel = async () => {
    setCancelling(true);
    setCancelError(null);
    try {
      await session.cancel();
      setConfirmOpen(false);
    } catch (caught) {
      setCancelError(caught instanceof Error ? caught.message : t("session.cancelFailed"));
    } finally {
      setCancelling(false);
    }
  };

  return (
    <>
      <header className="border-b bg-canvas/94 px-5 pb-3 pt-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <button
            className="icon-btn lg:hidden"
            onClick={onToggleSidebar}
            aria-label={t("app.openNavigation")}
          >
            <PanelLeft size={16} />
          </button>
          <div className="flex min-w-0 flex-1 items-center gap-2 text-[11px] text-subtle">
            <span className="truncate font-medium text-muted">{session.repository}</span>
            <span>/</span>
            <span>{t("session.repairSession")}</span>
            {session.model || session.simulatedLlm ? (
              <>
                <span className="hidden sm:inline">·</span>
                <span className="hidden truncate sm:inline">
                  {session.model ?? t("session.fakeModel")}
                </span>
              </>
            ) : null}
          </div>
          <div className="flex items-center gap-1.5">
            <StatusBadge
              status={session.status}
              stopReason={session.stopReason}
              pulse
            />
            {session.readonly ? (
              <span className="rounded-full border px-2 py-1 text-[10.5px] text-muted">{t("session.readOnly")}</span>
            ) : null}
            <button
              className="icon-btn"
              onClick={() => void copyRun()}
              title={copied ? t("session.copied") : t("session.copyRun")}
              aria-label={t("session.copyRun")}
            >
              {copied ? <Check size={14} /> : <Clipboard size={14} />}
            </button>
          </div>
        </div>

        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="line-clamp-2 max-w-[900px] text-[18px] font-semibold leading-snug tracking-[-0.02em]">
              {title}
            </h1>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-subtle">
              <span className="font-mono">{session.runId ?? "—"}</span>
              <span>{t("session.base", { sha: session.baseCommit ? session.baseCommit.slice(0, 10) : "—" })}</span>
              <span>{formatDate(session.createdAt)}</span>
              {session.durationSeconds !== null ? <span>{duration(session.durationSeconds)}</span> : null}
              {session.stopReason ? (
                <span>{t("session.stop", { reason: reason ? t(reason) : session.stopReason })}</span>
              ) : null}
              {session.simulatedLlm || session.simulatedSandbox ? (
                <span className="inline-flex items-center gap-1 text-warning">
                  <ShieldQuestion size={11} /> {t("session.simulatedRun")}
                </span>
              ) : null}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <div className="flex items-center gap-0.5 rounded-lg border bg-surface p-0.5">
              {(["summary", "normal", "detailed"] as Density[]).map((option) => (
                <button
                  key={option}
                  className="rounded-md px-2 py-1 text-[10.5px] text-muted transition-colors data-[active=true]:bg-elevated data-[active=true]:text-ink"
                  data-active={density === option}
                  onClick={() => onDensity(option)}
                >
                  {t(`session.${option}`)}
                </button>
              ))}
            </div>
            <button
              className="icon-btn"
              onClick={onToggleDetail}
              title={detailOpen ? t("session.hideDetails") : t("session.showDetails")}
              aria-label={detailOpen ? t("session.hideDetails") : t("session.showDetails")}
              aria-pressed={detailOpen}
            >
              {detailOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
            </button>
            {!session.readonly && !terminal ? (
              <Button
                variant="danger"
                size="sm"
                icon={<Square size={12} />}
                onClick={() => setConfirmOpen(true)}
                disabled={!canCancel}
              >
                {session.cancelRequested ? t("session.cancelling") : t("session.cancel")}
              </Button>
            ) : null}
            {session.task?.report_available || session.report ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onOpenPanel("run")}
              >
                {t("session.runDetails")}
              </Button>
            ) : null}
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2">
          {session.connection === "reconnecting" ? (
            <Notice
              tone="warning"
              action={
                <button className="text-[11px] underline" onClick={session.retryStream}>
                  {t("session.reconnect")}
                </button>
              }
            >
              {t("session.streamInterrupted")}
            </Notice>
          ) : null}
          {session.connection === "error" || session.streamError ? (
            <Notice
              tone="danger"
              action={
                <button className="text-[11px] underline" onClick={session.retryStream}>
                  {t("common.retry")}
                </button>
              }
            >
              {session.streamError ?? t("session.streamUnavailable")}
            </Notice>
          ) : null}
          {session.connection === "ended" ? (
            <span className="inline-flex items-center gap-1 text-[10.5px] text-subtle">
              <Wifi size={11} /> {t("session.streamClosed")}
            </span>
          ) : null}
          {session.connection === "live" ? (
            <span className="inline-flex items-center gap-1 text-[10.5px] text-success">
              <span className="h-1.5 w-1.5 animate-soft-pulse rounded-full bg-success" />
              {t("session.live")}
            </span>
          ) : null}
          {session.connection === "reconnecting" ? (
            <span className="inline-flex items-center gap-1 text-[10.5px] text-warning">
              <WifiOff size={11} /> {t("session.reconnecting")}
            </span>
          ) : null}
        </div>
      </header>

      <Modal
        open={confirmOpen}
        title={t("session.cancelTitle")}
        description={t("session.cancelDescription")}
        onClose={() => setConfirmOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              {t("session.keepRunning")}
            </Button>
            <Button
              variant="danger"
              loading={cancelling}
              onClick={() => void cancel()}
              icon={<Square size={12} />}
            >
              {t("session.cancelRun")}
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-[12.5px] leading-relaxed text-muted">
          <p>{t("session.cancelCooperative")}</p>
          {cancelError ? <Notice tone="danger">{cancelError}</Notice> : null}
        </div>
      </Modal>
    </>
  );
}
