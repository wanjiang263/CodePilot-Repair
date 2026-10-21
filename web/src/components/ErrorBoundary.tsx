import { Component, type ErrorInfo, type ReactNode } from "react";

import i18n from "../i18n";

export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("dashboard render failed", error, info);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas px-6">
        <div className="w-full max-w-lg rounded-2xl border bg-surface p-6">
          <div className="text-[13px] font-semibold">{i18n.t("errors.renderTitle")}</div>
          <p className="mt-2 whitespace-pre-wrap break-words font-mono text-[11px] text-danger">
            {this.state.error.message}
          </p>
          <button className="btn mt-4" onClick={() => window.location.reload()}>
            {i18n.t("errors.reload")}
          </button>
        </div>
      </div>
    );
  }
}
