import { Check, Clipboard, FileText } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SessionData } from "../../hooks/useSessionData";
import { EmptyState } from "../ui/EmptyState";

export function ReportPanel({ session }: { session: SessionData }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const markdown = session.report?.markdown ?? "";
  if (!markdown.trim()) {
    return (
      <EmptyState
        icon={<FileText size={16} />}
        title={t("panels.noReportMarkdown")}
        description={t("panels.noReportMarkdownDescription")}
      />
    );
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span className="text-[11px] font-medium">{t("panels.runReport")}</span>
        <span className="text-[10px] text-subtle">{t("panels.chars", { count: markdown.length.toLocaleString() })}</span>
        <button
          className="ml-auto inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] text-muted hover:bg-elevated"
          onClick={() => void copy()}
        >
          {copied ? <Check size={10} className="text-success" /> : <Clipboard size={10} />}
          {copied ? t("common.copied") : t("common.copy")}
        </button>
      </div>
      <pre className="terminal-surface min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words px-3 py-3 font-mono text-[10.5px] leading-relaxed scroll-thin">
        {markdown}
      </pre>
    </div>
  );
}
