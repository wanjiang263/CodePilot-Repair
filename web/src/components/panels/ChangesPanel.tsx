import { clsx } from "clsx";
import {
  Check,
  ChevronDown,
  Clipboard,
  FileMinus2,
  FilePenLine,
  FilePlus2,
  ShieldCheck,
  ShieldX,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import type { PatchFile } from "../../api/types";
import type { SessionData } from "../../hooks/useSessionData";
import { EmptyState } from "../ui/EmptyState";
import { Notice } from "../ui/Notice";
import { DiffViewer, type DiffMode } from "../DiffViewer";

function FileIcon({ status }: { status: PatchFile["status"] }) {
  if (status === "added") return <FilePlus2 size={13} className="text-success" />;
  if (status === "deleted") return <FileMinus2 size={13} className="text-danger" />;
  return <FilePenLine size={13} className="text-muted" />;
}

export function ChangesPanel({
  session,
  width,
  focusFile,
}: {
  session: SessionData;
  width: number;
  focusFile?: string | null;
}) {
  const { t } = useTranslation();
  const patch = session.patch;
  const files = useMemo(() => patch?.files ?? [], [patch]);
  const [selected, setSelected] = useState<string>("");
  const [mode, setMode] = useState<DiffMode>("unified");
  const [filter, setFilter] = useState("");
  const [copied, setCopied] = useState(false);

  // The selection is derived during render so it can never point at a file
  // that is no longer part of the patch (replaces the old sync effect).
  const effectiveSelected = useMemo(() => {
    if (!files.length) return "";
    if (focusFile && files.some((file) => file.path === focusFile)) return focusFile;
    if (files.some((file) => file.path === selected)) return selected;
    return files[0].path;
  }, [files, selected, focusFile]);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? files.filter((file) => file.path.toLowerCase().includes(needle)) : files;
  }, [files, filter]);

  const file = files.find((item) => item.path === effectiveSelected) ?? files[0] ?? null;
  const wide = width >= 520;

  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  if (!patch) {
    if (!session.status || ["queued", "running"].includes(session.status)) {
      return (
        <div className="space-y-3 p-3">
          <div className="skeleton h-7 w-2/3" />
          <div className="skeleton h-24 w-full" />
          <p className="px-1 text-[11.5px] leading-relaxed text-subtle">
            <Trans
              i18nKey="panels.finalPatchHint"
              components={{ code: <span className="font-mono" /> }}
            />
          </p>
        </div>
      );
    }
    return (
      <EmptyState
        title={t("panels.noPatch")}
        description={t("panels.noPatchDescription")}
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b px-3 py-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11.5px] font-medium">{t("panels.candidatePatch")}</span>
          <span className="text-[10.5px] text-subtle">
            {t("panels.fileCount", { count: patch.files_count })} · +{patch.additions} −{patch.deletions}
          </span>
          <div className="ml-auto flex items-center gap-1 rounded-md border bg-surface p-0.5">
            {(["unified", "split"] as DiffMode[]).map((option) => (
              <button
                key={option}
                className="rounded px-1.5 py-0.5 text-[10px] text-muted data-[active=true]:bg-elevated data-[active=true]:text-ink"
                data-active={mode === option}
                onClick={() => setMode(option)}
              >
                {t(`panels.${option}`)}
              </button>
            ))}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-1.5">
          <span
            className={clsx(
              "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]",
              patch.identity_verified
                ? "border-success/25 bg-success/10 text-success"
                : "border-warning/25 bg-warning/10 text-warning",
            )}
            title={patch.identity_note ?? undefined}
          >
            {patch.identity_verified ? <ShieldCheck size={11} /> : <ShieldX size={11} />}
            {patch.identity_verified ? t("panels.identityVerified") : t("panels.identityNotVerified")}
          </span>
          <button
            className="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] text-muted hover:bg-elevated"
            onClick={() => void copy(patch.sha256)}
            title={t("panels.copySha")}
          >
            <Clipboard size={10} /> {patch.sha256.slice(0, 16)}
          </button>
          {copied ? <Check size={11} className="text-success" /> : null}
          {patch.simulated ? (
            <span className="rounded border px-1.5 py-0.5 text-[10px] text-warning">{t("panels.simulated")}</span>
          ) : null}
        </div>
        {!patch.identity_verified ? (
          <div className="mt-2">
            <Notice tone="warning">{patch.identity_note ?? t("panels.identityNote")}</Notice>
          </div>
        ) : null}
        {patch.diff_truncated ? (
          <div className="mt-2">
            <Notice tone="warning">{t("panels.diffTruncated")}</Notice>
          </div>
        ) : null}
      </div>

      <div className={clsx("min-h-0 flex-1", wide ? "grid grid-cols-[190px_1fr]" : "flex flex-col")}>
        <div className={clsx("border-b bg-canvas/40", wide && "border-b-0 border-r")}>
          <div className="p-2">
            {wide ? (
              <input
                className="input h-7 py-0 text-[11px]"
                placeholder={t("panels.filterFiles")}
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                aria-label={t("panels.filterFiles")}
              />
            ) : (
              <div className="relative">
                <select
                  className="select h-8 py-0 text-[11.5px]"
                  value={effectiveSelected}
                  onChange={(event) => setSelected(event.target.value)}
                  aria-label={t("panels.changedFile")}
                >
                  {files.map((item) => (
                    <option key={item.path} value={item.path}>
                      {item.path} (+{item.additions} −{item.deletions})
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
          {wide ? (
            <div className="max-h-[calc(100vh-280px)] overflow-y-auto scroll-thin px-1.5 pb-2">
              {filtered.map((item) => (
                <button
                  key={item.path}
                  onClick={() => setSelected(item.path)}
                  className={clsx(
                    "flex w-full items-start gap-1.5 rounded-md px-1.5 py-1.5 text-left transition-colors hover:bg-elevated",
                    effectiveSelected === item.path && "bg-accent-soft",
                  )}
                  title={item.path}
                >
                  <span className="mt-0.5 shrink-0">
                    <FileIcon status={item.status} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-[10.5px] leading-4">{item.path}</span>
                    <span className="text-[9.5px] text-subtle">
                      +{item.additions} −{item.deletions}
                    </span>
                  </span>
                </button>
              ))}
              {!filtered.length ? (
                <div className="px-2 py-4 text-[11px] text-subtle">{t("panels.noMatchingFiles")}</div>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="min-h-0 overflow-auto scroll-thin bg-surface">
          {file ? (
            <>
              <div className="sticky top-0 z-10 flex items-center gap-2 border-b bg-surface/95 px-3 py-1.5 backdrop-blur">
                <FileIcon status={file.status} />
                <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{file.path}</span>
                <span className="text-[10px] text-subtle">
                  +{file.additions} −{file.deletions}
                </span>
                <button
                  className="rounded p-1 text-subtle hover:bg-elevated hover:text-ink"
                  onClick={() => void copy(file.path)}
                  title={t("panels.copyPath")}
                >
                  <Clipboard size={11} />
                </button>
              </div>
              <DiffViewer diff={file.diff} mode={mode} />
            </>
          ) : (
            <div className="flex h-full items-center justify-center p-6 text-[11.5px] text-subtle">
              {t("panels.selectFile")}
            </div>
          )}
        </div>
      </div>

      {patch.reviewer_approved !== null || patch.validation_passed !== null ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t px-3 py-2 text-[10.5px] text-muted">
          <span className="inline-flex items-center gap-1">
            {t("panels.validator")}:{" "}
            <span className={patch.validation_passed ? "text-success" : "text-danger"}>
              {patch.validation_passed === null
                ? t("common.notExecuted")
                : patch.validation_passed
                  ? t("panels.passed")
                  : t("panels.rejected")}
            </span>
          </span>
          <span className="inline-flex items-center gap-1">
            {t("panels.reviewer")}:{" "}
            <span className={patch.reviewer_approved ? "text-success" : "text-warning"}>
              {patch.reviewer_approved === null
                ? t("common.notExecuted")
                : patch.reviewer_approved
                  ? t("panels.approved")
                  : t("panels.notApproved")}
            </span>
          </span>
          {patch.reviewed_patch_hash ? (
            <span className="font-mono" title={t("panels.reviewedHash", { hash: patch.reviewed_patch_hash })}>
              {t("panels.reviewedHash", { hash: patch.reviewed_patch_hash.slice(0, 10) })}
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="border-t px-3 py-1.5 text-[10px] text-subtle">
        <ChevronDown size={10} className="mr-1 inline" /> {t("panels.diffsReadonly")}
      </div>
    </div>
  );
}
