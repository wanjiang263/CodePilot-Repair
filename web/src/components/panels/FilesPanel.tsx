import { FileMinus2, FilePenLine, FilePlus2, ListTree } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { PatchFile } from "../../api/types";
import type { SessionData } from "../../hooks/useSessionData";
import { EmptyState } from "../ui/EmptyState";

function icon(status: PatchFile["status"]) {
  if (status === "added") return <FilePlus2 size={13} className="text-success" />;
  if (status === "deleted") return <FileMinus2 size={13} className="text-danger" />;
  return <FilePenLine size={13} className="text-muted" />;
}

export function FilesPanel({
  session,
  onOpenFile,
}: {
  session: SessionData;
  onOpenFile: (path: string) => void;
}) {
  const { t } = useTranslation();
  const files = session.patch?.files ?? [];
  const localization = session.report?.localization as
    | {
        files?: Array<{ path?: string; reason?: string }>;
        symbols?: Array<{ qualified_name?: string; file?: string; reason?: string }>;
      }
    | undefined;
  const localizedFiles = localization?.files ?? [];
  const localizedSymbols = localization?.symbols ?? [];
  if (!files.length && !localizedFiles.length) {
    return (
      <EmptyState
        icon={<ListTree size={16} />}
        title={t("panels.noChangedFiles")}
        description={t("panels.noChangedFilesDescription")}
      />
    );
  }
  const additions = files.reduce((sum, file) => sum + file.additions, 0);
  const deletions = files.reduce((sum, file) => sum + file.deletions, 0);
  return (
    <div className="p-3">
      {files.length ? (
        <>
          <div className="mb-2 flex items-center justify-between text-[10.5px] text-subtle">
            <span>{t("panels.fileCount", { count: files.length })}</span>
            <span>
              +{additions} −{deletions}
            </span>
          </div>
          <div className="divide-y rounded-xl border bg-surface">
            {files.map((file) => (
              <button
                key={file.path}
                className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-elevated"
                onClick={() => onOpenFile(file.path)}
              >
                {icon(file.status)}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-[11px]">{file.path}</span>
                  <span className="text-[10px] text-subtle">
                    {t(`fileStatus.${file.status}`, { defaultValue: file.status })}
                  </span>
                </span>
                <span className="shrink-0 text-[10px] text-success">+{file.additions}</span>
                <span className="shrink-0 text-[10px] text-danger">−{file.deletions}</span>
              </button>
            ))}
          </div>
        </>
      ) : null}

      {localizedFiles.length ? (
        <div className={files.length ? "mt-5" : ""}>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-subtle">
            {t("panels.localizedContext")}
          </div>
          <div className="space-y-1">
            {localizedFiles.map((file) => (
              <div key={file.path} className="rounded-lg border bg-canvas/50 px-2.5 py-2">
                <div className="truncate font-mono text-[10.5px]">{file.path}</div>
                {file.reason ? (
                  <div className="mt-0.5 text-[10px] leading-relaxed text-subtle">{file.reason}</div>
                ) : null}
              </div>
            ))}
          </div>
          {localizedSymbols.length ? (
            <div className="mt-2 space-y-0.5">
              {localizedSymbols.slice(0, 6).map((symbol) => (
                <div key={`${symbol.file}-${symbol.qualified_name}`} className="truncate text-[10px] text-muted">
                  <span className="font-mono">{symbol.qualified_name}</span>
                  {symbol.reason ? <span className="text-subtle"> — {symbol.reason}</span> : null}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <p className="mt-3 text-[10.5px] leading-relaxed text-subtle">
        {t("panels.clickToJump")}
      </p>
    </div>
  );
}
