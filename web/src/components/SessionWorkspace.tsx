import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useSessionData } from "../hooks/useSessionData";
import type { Density } from "../lib/stages";
import { DetailPanel, type DetailTab } from "./DetailPanel";
import { SessionHeader } from "./SessionHeader";
import { Timeline } from "./Timeline";
import { Button } from "./ui/Button";
import { Notice } from "./ui/Notice";

export function SessionWorkspace({
  mode,
  taskId,
  benchId,
  apiKey,
  onSessionChange,
  onToggleSidebar,
  globalDetailOpen,
  onGlobalDetailOpen,
  overlay,
  onBack,
}: {
  mode: "live" | "replay";
  taskId?: string;
  benchId?: string;
  apiKey: string;
  onSessionChange: () => void;
  onToggleSidebar: () => void;
  globalDetailOpen: boolean;
  onGlobalDetailOpen: (open: boolean) => void;
  overlay: boolean;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const session = useSessionData({ mode, apiKey, taskId, benchId, onSessionChange });
  const [density, setDensity] = useState<Density>(() => {
    const stored = window.localStorage.getItem("codepilot.density");
    return stored === "summary" || stored === "normal" || stored === "detailed"
      ? stored
      : "normal";
  });
  const [tab, setTab] = useState<DetailTab>("changes");
  const [width, setWidth] = useState(() => Number(window.localStorage.getItem("codepilot.panelWidth") ?? 472));
  const [localDetailOpen, setLocalDetailOpen] = useState(!overlay);
  const detailOpen = globalDetailOpen && localDetailOpen;

  useEffect(() => {
    window.localStorage.setItem("codepilot.panelWidth", String(width));
  }, [width]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "j") {
        event.preventDefault();
        setLocalDetailOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const openPanel = (next: DetailTab) => {
    setTab(next);
    setLocalDetailOpen(true);
    onGlobalDetailOpen(true);
  };

  if (!taskId && !benchId) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <Notice tone="danger">{t("session.noSessionId")}</Notice>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <SessionHeader
        session={session}
        density={density}
        onDensity={setDensity}
        detailOpen={detailOpen}
        onToggleDetail={() => {
          setLocalDetailOpen((value) => !value);
          onGlobalDetailOpen(true);
        }}
        onOpenPanel={openPanel}
        onToggleSidebar={onToggleSidebar}
      />
      <div className="relative flex min-h-0 flex-1">
        {session.loadError ? (
          <div className="flex flex-1 items-center justify-center p-8">
            <div className="max-w-md space-y-3">
              <Notice tone="danger">{session.loadError}</Notice>
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" onClick={onBack} icon={<ArrowLeft size={13} />}>
                  {t("common.back")}
                </Button>
                <Button size="sm" onClick={() => void session.reload()} icon={<RefreshCw size={13} />}>
                  {t("common.retry")}
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <>
            <Timeline session={session} density={density} onOpenPanel={openPanel} />
            <DetailPanel
              session={session}
              open={detailOpen}
              tab={tab}
              onTab={setTab}
              width={width}
              onWidth={setWidth}
              onClose={() => setLocalDetailOpen(false)}
              overlay={overlay}
            />
          </>
        )}
      </div>
    </div>
  );
}
