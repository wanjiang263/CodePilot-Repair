import type {
  Benchmark,
  CancelResponse,
  CatalogTemplate,
  CleanupResult,
  EventRecord,
  HistoryItem,
  LiveModel,
  Meta,
  Patch,
  ProviderTestResult,
  Report,
  RuntimeFacts,
  SettingsDocument,
  StreamControl,
  Task,
  TaskDetail,
  TaskList,
} from "./types";

const API_BASE = `${(import.meta.env.VITE_API_BASE as string | undefined) ?? ""}/api/v1`;

export class ApiError extends Error {
  status: number;
  code: string;
  traceId?: string;

  constructor(status: number, code: string, message: string, traceId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`;
  let message = response.statusText || "Request failed";
  let traceId: string | undefined;
  try {
    const payload = (await response.json()) as {
      error?: { code?: string; message?: string; trace_id?: string };
    };
    if (payload.error) {
      code = payload.error.code ?? code;
      message = payload.error.message ?? message;
      traceId = payload.error.trace_id;
    }
  } catch {
    // The response was not JSON; keep the status text.
  }
  return new ApiError(response.status, code, message, traceId);
}

export interface RequestOptions {
  key: string;
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(path: string, options: RequestOptions): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "X-API-Key": options.key,
      ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function getMeta(key: string, signal?: AbortSignal): Promise<Meta> {
  return request<Meta>("/meta", { key, signal });
}

export function createTask(
  key: string,
  body: {
    repository: { kind: "https" | "demo"; value: string };
    issue: string;
    base_commit?: string;
    execution_mode?: "offline_fake" | "docker" | "live";
    budget?: Record<string, unknown>;
    model?: { provider_id: string; model_id: string };
  },
): Promise<Task> {
  return request<Task>("/tasks", { key, method: "POST", body });
}

export function listTasks(
  key: string,
  params: { limit?: number; offset?: number; status?: string; q?: string; repository?: string } = {},
  signal?: AbortSignal,
): Promise<TaskList> {
  const query = new URLSearchParams();
  if (params.limit) query.set("limit", String(params.limit));
  if (params.offset) query.set("offset", String(params.offset));
  if (params.status) query.set("status", params.status);
  if (params.q) query.set("q", params.q);
  if (params.repository) query.set("repository", params.repository);
  const queryString = query.toString();
  const suffix = queryString ? `?${queryString}` : "";
  return request<TaskList>(`/tasks${suffix}`, { key, signal });
}

export function getTask(key: string, taskId: string, signal?: AbortSignal): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`, { key, signal });
}

export function getPatch(key: string, runId: string, signal?: AbortSignal): Promise<Patch> {
  return request<Patch>(`/runs/${encodeURIComponent(runId)}/patch`, { key, signal });
}

export function getReport(key: string, runId: string, signal?: AbortSignal): Promise<Report> {
  return request<Report>(`/runs/${encodeURIComponent(runId)}/report`, { key, signal });
}

export function cancelRun(key: string, runId: string): Promise<CancelResponse> {
  return request<CancelResponse>(`/runs/${encodeURIComponent(runId)}/cancel`, {
    key,
    method: "POST",
  });
}

export function listHistory(key: string, q?: string, signal?: AbortSignal): Promise<HistoryItem[]> {
  const suffix = q ? `?q=${encodeURIComponent(q)}` : "";
  return request<HistoryItem[]>(`/history${suffix}`, { key, signal });
}

export function getBenchmark(key: string, benchId: string, signal?: AbortSignal): Promise<Benchmark> {
  return request<Benchmark>(`/benchmarks/${encodeURIComponent(benchId)}`, { key, signal });
}

export function getBenchmarkList(key: string, signal?: AbortSignal): Promise<HistoryItem[]> {
  return request<HistoryItem[]>("/benchmarks", { key, signal });
}

interface StreamOptions {
  key: string;
  runId: string;
  afterSeq?: number;
  signal: AbortSignal;
  onEvent: (event: EventRecord) => void;
  onControl: (control: StreamControl) => void;
  onOpen?: () => void;
}

/**
 * Consume the authenticated SSE endpoint with fetch. Native EventSource cannot
 * attach `X-API-Key`, and putting the key in a URL would leak it into logs.
 */
export async function streamRunEvents(options: StreamOptions): Promise<void> {
  const headers: Record<string, string> = {
    "X-API-Key": options.key,
    Accept: "text/event-stream",
  };
  if (options.afterSeq && options.afterSeq > 0) {
    headers["Last-Event-ID"] = String(options.afterSeq);
  }
  const response = await fetch(
    `${API_BASE}/runs/${encodeURIComponent(options.runId)}/events?after_seq=${options.afterSeq ?? 0}`,
    { headers, signal: options.signal },
  );
  if (!response.ok) throw await parseError(response);
  options.onOpen?.();
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      handleBlock(block, options);
      boundary = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) handleBlock(buffer, options);
}

export function handleBlock(block: string, options: Omit<StreamOptions, "signal" | "onOpen">): void {
  if (!block.trim()) return;
  let eventName = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return;
  let payload: unknown;
  try {
    payload = JSON.parse(data.join("\n"));
  } catch {
    return;
  }
  if (eventName === "control") {
    options.onControl(payload as StreamControl);
  } else if (eventName === "repair") {
    options.onEvent(payload as EventRecord);
  }
}

export { API_BASE };

export function getSettings(key: string, signal?: AbortSignal): Promise<SettingsDocument> {
  return request<SettingsDocument>("/settings", { key, signal });
}

export function updateSettingsSection<T extends Record<string, unknown>>(
  key: string,
  section: string,
  values: T,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(`/settings/sections/${encodeURIComponent(section)}`, {
    key,
    method: "PUT",
    body: { version, values },
  });
}

export function getCatalog(key: string, signal?: AbortSignal): Promise<CatalogTemplate[]> {
  return request<CatalogTemplate[]>("/settings/catalog", { key, signal });
}

export function createProvider(
  key: string,
  provider: Record<string, unknown>,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>("/settings/providers", {
    key,
    method: "POST",
    body: { version, provider },
  });
}

export function createProviderFromTemplate(
  key: string,
  body: {
    catalog_id: string;
    provider_id: string;
    name?: string;
    base_url?: string;
    region?: string;
    api_key_env?: string;
  },
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>("/settings/providers/from-template", {
    key,
    method: "POST",
    body: { version, ...body },
  });
}

export function updateProvider(
  key: string,
  providerId: string,
  provider: Record<string, unknown>,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(`/settings/providers/${encodeURIComponent(providerId)}`, {
    key,
    method: "PUT",
    body: { version, provider },
  });
}

export function deleteProvider(
  key: string,
  providerId: string,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(
    `/settings/providers/${encodeURIComponent(providerId)}?version=${version}`,
    { key, method: "DELETE" },
  );
}

export function setProviderCredential(
  key: string,
  providerId: string,
  body: { mode: "value"; value: string } | { mode: "env"; env_name: string },
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(
    `/settings/providers/${encodeURIComponent(providerId)}/credential`,
    { key, method: "POST", body: { version, ...body } },
  );
}

export function deleteProviderCredential(
  key: string,
  providerId: string,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(
    `/settings/providers/${encodeURIComponent(providerId)}/credential?version=${version}`,
    { key, method: "DELETE" },
  );
}

export function upsertProviderModel(
  key: string,
  providerId: string,
  model: Record<string, unknown>,
  version: number,
  originalModelId?: string | null,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(
    `/settings/providers/${encodeURIComponent(providerId)}/models`,
    {
      key,
      method: "POST",
      body: { version, model, original_model_id: originalModelId ?? null },
    },
  );
}

export function deleteProviderModel(
  key: string,
  providerId: string,
  modelId: string,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>(
    `/settings/providers/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}?version=${version}`,
    { key, method: "DELETE" },
  );
}

export function testProvider(
  key: string,
  providerId: string,
  body: { kind: "config" | "models" | "inference"; model_id?: string; confirm_cost?: boolean },
): Promise<ProviderTestResult> {
  return request<ProviderTestResult>(
    `/settings/providers/${encodeURIComponent(providerId)}/test`,
    { key, method: "POST", body },
  );
}

export function discoverProviderModels(
  key: string,
  providerId: string,
): Promise<ProviderTestResult> {
  return request<ProviderTestResult>(
    `/settings/providers/${encodeURIComponent(providerId)}/discover`,
    { key, method: "POST" },
  );
}

export function getAvailableModels(key: string, signal?: AbortSignal): Promise<LiveModel[]> {
  return request<LiveModel[]>("/settings/models", { key, signal });
}

export function exportSettings(key: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/settings/export", { key });
}

export function importSettings(
  key: string,
  payload: Record<string, unknown>,
  version: number,
): Promise<SettingsDocument> {
  return request<SettingsDocument>("/settings/import", {
    key,
    method: "POST",
    body: { version, payload },
  });
}

export function getRuntimeFacts(key: string, signal?: AbortSignal): Promise<RuntimeFacts> {
  return request<RuntimeFacts>("/settings/runtime", { key, signal });
}

export function cleanupTasks(
  key: string,
  taskIds: string[],
  version: number,
): Promise<CleanupResult> {
  return request<CleanupResult>("/settings/cleanup", {
    key,
    method: "POST",
    body: { version, task_ids: taskIds, confirmation: "DELETE" },
  });
}

export function getSettingsLogs(
  key: string,
  limit = 80,
  signal?: AbortSignal,
): Promise<import("./types").SettingsLogEntry[]> {
  return request<import("./types").SettingsLogEntry[]>(`/settings/logs?limit=${limit}`, {
    key,
    signal,
  });
}

export function getDocumentation(
  key: string,
  name: string,
  signal?: AbortSignal,
): Promise<{ name: string; content: string }> {
  return request<{ name: string; content: string }>(`/settings/docs/${encodeURIComponent(name)}`, {
    key,
    signal,
  });
}
