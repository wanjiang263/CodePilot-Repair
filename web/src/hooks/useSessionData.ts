import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import i18n from "../i18n";
import {
  ApiError,
  cancelRun,
  getBenchmark,
  getPatch,
  getReport,
  getTask,
} from "../api/client";
import type {
  Benchmark,
  Budget,
  EventRecord,
  OfficialVerdict,
  Patch,
  Report,
  TaskDetail,
  UiStatus,
  Usage,
} from "../api/types";
import { useRunStream, type ConnectionState } from "./useRunStream";

export interface SessionData {
  mode: "live" | "replay";
  runId: string | null;
  taskId: string | null;
  benchId: string | null;
  repository: string;
  baseCommit: string;
  model: string | null;
  issue: string;
  createdAt: string | null;
  status: UiStatus;
  rawStatus: string;
  stopReason: string | null;
  verificationLevel: string | null;
  official: OfficialVerdict | null;
  internal: Report["internal"] | null;
  usage: Usage;
  budget: Budget;
  report: Report | null;
  patch: Patch | null;
  task: TaskDetail | null;
  benchmark: Benchmark | null;
  events: EventRecord[];
  connection: ConnectionState;
  streamError: string | null;
  loadError: string | null;
  loading: boolean;
  readonly: boolean;
  simulatedLlm: boolean;
  simulatedSandbox: boolean;
  durationSeconds: number | null;
  attempts: number;
  cancelRequested: boolean;
  cancelling: boolean;
  cancel: () => Promise<void>;
  retryStream: () => void;
  reload: () => Promise<void>;
}

const EMPTY_USAGE: Usage = {
  input_tokens: 0,
  output_tokens: 0,
  cached_input_tokens: 0,
  cost: 0,
  currency: "CNY",
  llm_calls: 0,
  cost_known: true,
};

export function useSessionData(options: {
  mode: "live" | "replay";
  apiKey: string;
  taskId?: string | null;
  benchId?: string | null;
  onSessionChange?: () => void;
}): SessionData {
  const { mode, apiKey, taskId, benchId, onSessionChange } = options;
  const [taskState, setTaskState] = useState<TaskDetail | null>(null);
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [patch, setPatch] = useState<Patch | null>(null);
  const [loading, setLoading] = useState(mode === "live" ? Boolean(taskId) : Boolean(benchId));
  const [loadError, setLoadError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const fetchedTerminal = useRef<string | null>(null);

  const sessionKey = `${mode}|${taskId ?? ""}|${benchId ?? ""}|${apiKey}`;
  const [lastSessionKey, setLastSessionKey] = useState(sessionKey);

  // Reset the session state synchronously during render when the session
  // identity changes; the fetch effect below re-runs on the same keys and also
  // clears the one-time terminal-fetch bookkeeping ref.
  if (lastSessionKey !== sessionKey) {
    setLastSessionKey(sessionKey);
    setTaskState(null);
    setBenchmark(null);
    setReport(null);
    setPatch(null);
    setLoadError(null);
    setLoading(mode === "live" ? Boolean(taskId) : Boolean(benchId));
  }

  const runId = taskState?.run_id ?? benchmark?.run_id ?? null;
  const stream = useRunStream(runId, apiKey, Boolean(runId));

  useEffect(() => {
    fetchedTerminal.current = null;
    if (mode === "live" && !taskId) return;
    if (mode === "replay" && !benchId) return;
    const controller = new AbortController();
    const load = async () => {
      try {
        if (mode === "live" && taskId) {
          const detail = await getTask(apiKey, taskId, controller.signal);
          setTaskState(detail);
          if (detail.report_available) {
            await Promise.all([
              getReport(apiKey, detail.run_id, controller.signal)
                .then(setReport)
                .catch(() => undefined),
              getPatch(apiKey, detail.run_id, controller.signal)
                .then(setPatch)
                .catch(() => undefined),
            ]);
          }
        } else if (mode === "replay" && benchId) {
          const item = await getBenchmark(apiKey, benchId, controller.signal);
          setBenchmark(item);
          const [loadedReport, loadedPatch] = await Promise.all([
            getReport(apiKey, item.run_id, controller.signal).catch(() => null),
            getPatch(apiKey, item.run_id, controller.signal).catch(() => null),
          ]);
          setReport(loadedReport);
          setPatch(loadedPatch);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setLoadError(
            error instanceof ApiError ? `${error.message}${error.traceId ? ` (${error.traceId})` : ""}` : i18n.t("session.loadFailed"),
          );
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => controller.abort();
  }, [mode, apiKey, taskId, benchId, reloadToken]);

  const refreshTerminal = useCallback(
    async (effectiveRunId: string) => {
      try {
        if (mode === "live") {
          const detailTask = taskId ? await getTask(apiKey, taskId) : null;
          if (detailTask) setTaskState(detailTask);
        }
        const [loadedReport, loadedPatch] = await Promise.all([
          getReport(apiKey, effectiveRunId).catch(() => null),
          getPatch(apiKey, effectiveRunId).catch(() => null),
        ]);
        if (loadedReport) setReport(loadedReport);
        if (loadedPatch) setPatch(loadedPatch);
        if (mode === "replay" && benchId) {
          const item = await getBenchmark(apiKey, benchId).catch(() => null);
          if (item) setBenchmark(item);
        }
        onSessionChange?.();
      } catch {
        // The terminal event is already visible; the panels can show a retry.
      }
    },
    [apiKey, benchId, mode, onSessionChange, taskId],
  );

  // Terminal control messages are merged into the exposed task during render;
  // the effect below only triggers the one-time terminal refetch.
  const task = useMemo(() => {
    if (!taskState) return taskState;
    if (!stream.control) return taskState;
    return {
      ...taskState,
      status: stream.control.status ?? taskState.status,
      raw_status: stream.control.status ?? taskState.raw_status,
      ui_status: stream.control.ui_status ?? taskState.ui_status,
      stop_reason: stream.control.stop_reason ?? taskState.stop_reason,
    };
  }, [taskState, stream.control]);

  useEffect(() => {
    if (!stream.control || !runId) return;
    if (fetchedTerminal.current === runId) return;
    fetchedTerminal.current = runId;
    (async () => {
      await refreshTerminal(runId);
    })();
  }, [stream.control, runId, refreshTerminal]);

  const cancel = useCallback(async () => {
    if (!runId || mode !== "live") return;
    setCancelling(true);
    try {
      await cancelRun(apiKey, runId);
      if (taskId) {
        const refreshed = await getTask(apiKey, taskId).catch(() => null);
        if (refreshed) setTaskState(refreshed);
      }
      onSessionChange?.();
    } finally {
      setCancelling(false);
    }
  }, [apiKey, mode, onSessionChange, runId, taskId]);

  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    setReloadToken((value) => value + 1);
  }, []);

  const derived = useMemo(() => {
    const workerStarted = stream.events.some((event) => event.type === "worker_started");
    let status: UiStatus =
      stream.control?.ui_status ??
      task?.ui_status ??
      benchmark?.ui_status ??
      report?.ui_status ??
      "queued";
    if (status === "queued" && workerStarted) status = "running";
    const rawStatus =
      stream.control?.status ??
      task?.raw_status ??
      benchmark?.raw_status ??
      report?.raw_status ??
      "unknown";
    const stopReason =
      stream.control?.stop_reason ??
      task?.stop_reason ??
      benchmark?.stop_reason ??
      report?.stop_reason ??
      null;
    const summary = report?.summary ?? {};
    return {
      status,
      rawStatus,
      stopReason,
      usage: report?.usage ?? benchmark?.usage ?? task?.usage ?? EMPTY_USAGE,
      budget: report?.budget ?? task?.budget ?? benchmark?.budget ?? {},
      verificationLevel:
        report?.verification_level ??
        task?.verification_level ??
        benchmark?.internal.verification_level ??
        null,
      official: benchmark?.official ?? report?.official ?? null,
      internal: report?.internal ?? benchmark?.internal ?? null,
      simulatedLlm: Boolean(task?.simulated_llm ?? summary.simulated_llm ?? false),
      simulatedSandbox: Boolean(task?.simulated_sandbox ?? summary.simulated_sandbox ?? false),
      durationSeconds:
        task?.duration_seconds ??
        (typeof summary.runtime_seconds === "number" ? summary.runtime_seconds : null),
      attempts:
        task?.attempts ??
        (typeof summary.attempts === "number" ? summary.attempts : (report?.attempts.length ?? 0)),
      cancelRequested: Boolean(task?.cancel_requested),
      issue: task?.issue ?? report?.issue ?? "",
      repository:
        task?.repository ?? benchmark?.repository ?? report?.repository ?? "unknown repository",
      baseCommit: task?.base_commit ?? benchmark?.internal.closure?.base_commit ?? report?.base_commit ?? "",
      model:
        benchmark?.model ??
        task?.model_identity ??
        (task?.model_id ? `${task.provider_id ?? ""}/${task.model_id}` : null),
      createdAt: task?.created_at ?? benchmark?.recorded_at ?? null,
    };
  }, [benchmark, report, stream.control, stream.events, task]);

  return {
    mode,
    runId,
    taskId: taskId ?? null,
    benchId: benchId ?? null,
    repository: String(derived.repository),
    baseCommit: String(derived.baseCommit ?? ""),
    model: derived.model,
    issue: String(derived.issue ?? ""),
    createdAt: derived.createdAt,
    status: derived.status,
    rawStatus: derived.rawStatus,
    stopReason: derived.stopReason,
    verificationLevel: derived.verificationLevel,
    official: derived.official,
    internal: derived.internal,
    usage: derived.usage,
    budget: derived.budget,
    report,
    patch,
    task,
    benchmark,
    events: stream.events,
    connection: stream.connection,
    streamError: stream.error,
    loadError,
    loading,
    readonly: mode === "replay",
    simulatedLlm: derived.simulatedLlm,
    simulatedSandbox: derived.simulatedSandbox,
    durationSeconds: derived.durationSeconds,
    attempts: derived.attempts,
    cancelRequested: derived.cancelRequested,
    cancelling,
    cancel,
    retryStream: stream.retryNow,
    reload,
  };
}
