import { clsx } from "clsx";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { parseDiff, toSplitRows, tokenizeLine, type DiffLine } from "../lib/diff";

export type DiffMode = "unified" | "split";

function Tokens({ text }: { text: string }) {
  return (
    <>
      {tokenizeLine(text).map((token, index) => (
        <span key={index} className={token.className}>
          {token.text}
        </span>
      ))}
    </>
  );
}

function sign(kind: DiffLine["kind"]): string {
  if (kind === "add") return "+";
  if (kind === "del") return "−";
  return " ";
}

function Unified({ lines }: { lines: DiffLine[] }) {
  return (
    <div className="min-w-max code-surface">
      {lines.map((line, index) => (
        <div
          key={index}
          className={clsx(
            "diff-line flex",
            line.kind === "add" && "diff-add",
            line.kind === "del" && "diff-del",
            line.kind === "hunk" && "diff-hunk",
            line.kind === "meta" && "diff-meta",
          )}
        >
          <span className="w-10 shrink-0 select-none pr-2 text-right text-[10px] text-subtle">
            {line.oldNo ?? ""}
          </span>
          <span className="w-10 shrink-0 select-none pr-2 text-right text-[10px] text-subtle">
            {line.newNo ?? ""}
          </span>
          <span
            className={clsx(
              "w-4 shrink-0 select-none text-center",
              line.kind === "add" && "text-success",
              line.kind === "del" && "text-danger",
            )}
          >
            {sign(line.kind)}
          </span>
          <span className="pr-3">
            <Tokens text={line.text} />
          </span>
        </div>
      ))}
    </div>
  );
}

function Half({ line, side }: { line: DiffLine | null; side: "left" | "right" }) {
  if (!line) {
    return <div className="min-h-[19px] flex-1 bg-canvas/30" />;
  }
  return (
    <div
      className={clsx(
        "diff-line flex min-w-0 flex-1",
        line.kind === "add" && "diff-add",
        line.kind === "del" && "diff-del",
      )}
    >
      <span className="w-9 shrink-0 select-none pr-2 text-right text-[10px] text-subtle">
        {(side === "left" ? line.oldNo : line.newNo) ?? ""}
      </span>
      <span
        className={clsx(
          "w-4 shrink-0 select-none text-center",
          line.kind === "add" && "text-success",
          line.kind === "del" && "text-danger",
        )}
      >
        {sign(line.kind)}
      </span>
      <span className="min-w-0 pr-3">
        <Tokens text={line.text} />
      </span>
    </div>
  );
}

export function DiffViewer({ diff, mode }: { diff: string; mode: DiffMode }) {
  const { t } = useTranslation();
  const lines = useMemo(() => parseDiff(diff), [diff]);
  const rows = useMemo(() => toSplitRows(lines), [lines]);
  if (!diff.trim()) {
    return <div className="px-4 py-8 text-center text-[12px] text-subtle">{t("panels.emptyDiff")}</div>;
  }
  if (mode === "split") {
    return (
      <div className="min-w-[560px] code-surface">
        {rows.map((row, index) => {
          if (row.meta) {
            return (
              <div
                key={index}
                className={clsx(
                  "diff-line",
                  row.meta.kind === "hunk" && "diff-hunk",
                  row.meta.kind === "meta" && "diff-meta",
                )}
              >
                {row.meta.text}
              </div>
            );
          }
          return (
            <div key={index} className="flex border-b border-line/40 last:border-0">
              <div className="flex min-w-0 flex-1 border-r border-line/60">
                <Half line={row.left} side="left" />
              </div>
              <div className="flex min-w-0 flex-1">
                <Half line={row.right} side="right" />
              </div>
            </div>
          );
        })}
      </div>
    );
  }
  return <Unified lines={lines} />;
}

