import { ArrowRight, Eye, EyeOff, KeyRound, LockKeyhole, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { Button } from "./ui/Button";

export function ConnectScreen({
  onConnect,
  initialError,
}: {
  onConnect: (key: string) => Promise<void>;
  initialError?: string | null;
}) {
  const { t } = useTranslation();
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(initialError ?? null);

  const submit = async () => {
    if (key.trim().length < 8) {
      setError(t("connect.shortKey"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await onConnect(key.trim());
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.status === 401
            ? t("connect.rejected")
            : caught.message
          : t("connect.unreachable"),
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-5 py-10">
      <div className="w-full max-w-[440px]">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-accent/20 bg-accent-soft text-accent">
            <svg width="24" height="24" viewBox="0 0 20 20" fill="none" aria-hidden>
              <path d="M5 4.5v11M15 4.5v11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <circle cx="5" cy="4.5" r="1.7" fill="currentColor" />
              <circle cx="15" cy="15.5" r="1.7" fill="currentColor" />
              <path d="M5 10h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </div>
          <div>
            <div className="text-[16px] font-semibold tracking-tight">CodePilot-Repair</div>
            <div className="text-[11px] text-muted">{t("connect.subtitle")}</div>
          </div>
        </div>

        <div className="panel p-5 shadow-soft">
          <div className="mb-3 flex items-center gap-2 text-[12px] font-medium">
            <KeyRound size={14} /> {t("connect.title")}
          </div>
          <p className="mb-4 text-[12px] leading-relaxed text-muted">
            <Trans
              i18nKey="connect.intro"
              components={{ code: <span className="font-mono" /> }}
            />
          </p>
          <label className="mb-1 block text-[11px] text-muted" htmlFor="connect-key">
            {t("connect.apiKey")}
          </label>
          <div className="relative">
            <input
              id="connect-key"
              className="input h-10 pr-10 font-mono text-[12px]"
              type={show ? "text" : "password"}
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder={t("connect.placeholder")}
              autoComplete="off"
              spellCheck={false}
              autoFocus
              onKeyDown={(event) => {
                if (event.key === "Enter") void submit();
              }}
            />
            <button
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-subtle hover:text-ink"
              onClick={() => setShow((value) => !value)}
              aria-label={show ? t("connect.hideKey") : t("connect.showKey")}
              type="button"
            >
              {show ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
          {error ? (
            <div className="mt-2 rounded-md border border-danger/25 bg-danger/10 px-2.5 py-2 text-[11px] text-danger">
              {error}
            </div>
          ) : null}
          <Button
            variant="primary"
            className="mt-3 w-full"
            onClick={() => void submit()}
            loading={loading}
            icon={<ArrowRight size={14} />}
          >
            {t("connect.submit")}
          </Button>
        </div>

        <div className="mt-5 space-y-2 text-[10.5px] leading-relaxed text-subtle">
          <div className="flex items-start gap-2">
            <LockKeyhole size={12} className="mt-0.5 shrink-0" />
            {t("connect.loopback")}
          </div>
          <div className="flex items-start gap-2">
            <ShieldCheck size={12} className="mt-0.5 shrink-0" />
            {t("connect.worker")}
          </div>
        </div>
      </div>
    </div>
  );
}
