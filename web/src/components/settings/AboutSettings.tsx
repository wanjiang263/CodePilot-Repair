import { BookOpen, ExternalLink, GitCommitHorizontal, Info, Scale } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getDocumentation } from "../../api/client";
import type { Meta, RuntimeFacts } from "../../api/types";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { SettingsSection } from "./shared";

const DOCS = [
  { id: "README.md", labelKey: "settings.about.readme" },
  { id: "architecture", labelKey: "settings.about.architectureDoc" },
  { id: "agents", labelKey: "settings.about.agentsDoc" },
  { id: "security", labelKey: "settings.about.securityDoc" },
];

export function AboutSettings({
  meta,
  runtime,
  apiKey,
}: {
  meta: Meta | null;
  runtime: RuntimeFacts | null;
  apiKey: string;
}) {
  const { t } = useTranslation();
  const version = runtime?.version ?? meta?.version ?? "0.1.0";
  const commit = runtime?.git_commit ?? null;
  const [doc, setDoc] = useState<{ name: string; content: string } | null>(null);
  const [loadingDoc, setLoadingDoc] = useState(false);
  const [docError, setDocError] = useState<string | null>(null);

  const openDoc = async (name: string) => {
    setLoadingDoc(true);
    setDocError(null);
    try {
      setDoc(await getDocumentation(apiKey, name));
    } catch (caught) {
      setDocError(caught instanceof Error ? caught.message : t("common.error"));
    } finally {
      setLoadingDoc(false);
    }
  };

  return (
    <div className="space-y-4">
      <SettingsSection
        title={t("settings.about.title")}
        description={t("settings.about.intro")}
        actions={<Info size={15} className="text-subtle" />}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
            <div className="text-[10px] uppercase tracking-[0.1em] text-subtle">{t("settings.about.version")}</div>
            <div className="mt-1 font-mono text-[13px]">{version}</div>
          </div>
          <div className="rounded-xl border bg-canvas/40 px-3 py-2.5">
            <div className="flex items-center gap-1 text-[10px] uppercase tracking-[0.1em] text-subtle">
              <GitCommitHorizontal size={11} /> {t("settings.about.commit")}
            </div>
            <div className="mt-1 font-mono text-[13px]">{commit ?? "—"}</div>
          </div>
        </div>
        <div className="mt-3 text-[11.5px] leading-relaxed text-muted">
          <div className="mb-1 font-medium">{t("settings.about.architecture")}</div>
          {t("settings.about.architectureValue")}
        </div>
        <div className="mt-3 rounded-lg border border-line bg-elevated px-3 py-2 text-[11px] leading-relaxed text-muted">
          {t("settings.about.disclaimer")}
        </div>
      </SettingsSection>

      <SettingsSection title={t("settings.about.docs")} actions={<BookOpen size={15} className="text-subtle" />}>
        <div className="grid gap-2 sm:grid-cols-2">
          {DOCS.map((item) => (
            <button
              key={item.id}
              className="flex items-center justify-between rounded-lg border bg-surface px-3 py-2 text-left text-[11.5px] text-muted transition-colors hover:bg-elevated hover:text-ink disabled:cursor-wait disabled:opacity-60"
              onClick={() => void openDoc(item.id)}
              disabled={loadingDoc}
            >
              {t(item.labelKey)}
              <ExternalLink size={12} />
            </button>
          ))}
        </div>
        <p className="mt-2 text-[10.5px] text-subtle">{t("settings.about.docsHint")}</p>
        {docError ? <div className="mt-2 text-[10.5px] text-danger">{docError}</div> : null}
      </SettingsSection>

      <SettingsSection title={t("settings.about.license")} actions={<Scale size={15} className="text-subtle" />}>
        <p className="text-[11.5px] leading-relaxed text-muted">{t("settings.about.licenseValue")}</p>
      </SettingsSection>

      <Modal
        open={Boolean(doc)}
        title={doc?.name ?? ""}
        onClose={() => setDoc(null)}
        width="max-w-4xl"
        footer={
          <Button variant="ghost" onClick={() => setDoc(null)}>
            {t("common.close")}
          </Button>
        }
      >
        <pre className="max-h-[68vh] overflow-auto whitespace-pre-wrap rounded-lg border bg-canvas/40 p-3 font-mono text-[10.5px] leading-relaxed text-muted">
          {doc?.content ?? ""}
        </pre>
      </Modal>
    </div>
  );
}
