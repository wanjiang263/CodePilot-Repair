export type UiStatus =
  | "queued"
  | "running"
  | "completed"
  | "stopped"
  | "failed"
  | "cancelled"
  | "error"
  | "uncertain";

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  cost: number;
  currency: string;
  llm_calls: number;
  cost_known: boolean;
}

export interface Budget {
  max_iterations?: number | null;
  max_test_runs?: number | null;
  max_files_changed?: number | null;
  max_lines_changed?: number | null;
  max_tool_calls_per_iteration?: number | null;
  max_review_calls?: number | null;
  max_runtime_seconds?: number | null;
  max_total_tokens?: number | null;
  max_cost?: number | null;
  budget_currency?: string;
}

export interface Task {
  runtime?: Record<string, any>;
  cancellation_state?: "not_requested" | "requested" | "confirmed";
  task_id: string;
  run_id: string;
  repository: string;
  repository_kind: string;
  issue_preview: string;
  status: string;
  raw_status: string;
  ui_status: UiStatus;
  stop_reason: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  execution_mode: string;
  base_commit: string;
  attempts: number;
  recovery_state: string | null;
  cancel_requested: boolean;
  usage: Usage;
  verification_level: string | null;
  patch_available: boolean;
  report_available: boolean;
  events_count: number;
  duration_seconds: number | null;
  simulated_llm: boolean;
  simulated_sandbox: boolean;
  official_verdict: string | null;
  llm_mode: "fake" | "live" | string;
  provider_id: string | null;
  model_id: string | null;
  model_identity: string | null;
  config_hash: string | null;
}

export interface TaskDetail extends Task {
  issue: string;
  budget: Budget;
  summary: Record<string, unknown>;
  error: string | null;
}

export interface TaskList {
  items: Task[];
  total: number;
  limit: number;
  offset: number;
}

export interface EventRecord {
  run_id: string;
  task_id: string;
  seq: number;
  type: string;
  node: string;
  timestamp: string | null;
  status?: string | null;
  summary?: string | null;
  duration_ms?: number | null;
  usage?: {
    model: string;
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    cost: number;
    cost_known: boolean;
    currency: string;
  } | null;
  tool_calls?: Array<Record<string, unknown>>;
  error?: string | null;
  extra?: Record<string, unknown>;
}

export interface PatchFile {
  path: string;
  additions: number;
  deletions: number;
  status: "added" | "modified" | "deleted" | string;
  diff: string;
}

export interface Patch {
  run_id: string;
  task_id: string;
  sha256: string;
  identity_sha256: string | null;
  size_bytes: number;
  files: PatchFile[];
  files_count: number;
  additions: number;
  deletions: number;
  diff: string;
  diff_truncated: boolean;
  validation_passed: boolean | null;
  reviewer_approved: boolean | null;
  reviewed_patch_hash: string | null;
  frozen_by_coding_stage: boolean | null;
  identity_verified: boolean;
  identity_note: string | null;
  simulated: boolean;
}

export interface OfficialVerdict {
  recorded: boolean;
  verdict: string | null;
  resolved: boolean | null;
  status: string | null;
  harness_status: string | null;
  authoritative: boolean;
  source: string | null;
  note: string | null;
}

export interface InternalVerification {
  status: string | null;
  raw_status: string | null;
  stop_reason: string | null;
  verification_level: string | null;
  review_executed: boolean | null;
  review_bound_to_candidate: boolean | null;
  regression_complete: boolean | null;
  regression_new_failures: number | null;
  target_tests_status: string | null;
  unexecuted_stages: string[];
  closure: Record<string, any>;
}

export interface ReviewIssue {
  severity: string;
  file: string;
  line?: number | null;
  description: string;
}

export interface Report {
  harness?: Record<string, any>;
  run_id: string;
  task_id: string;
  status: string;
  raw_status: string;
  ui_status: UiStatus;
  stop_reason: string | null;
  verification_level: string | null;
  official: OfficialVerdict;
  internal: InternalVerification;
  issue: string;
  repository: string;
  base_commit: string;
  summary: Record<string, any>;
  tests: {
    target_tests_executed?: boolean;
    target_tests_status?: string | null;
    target_test_sha256?: string;
    target_execution?: Record<string, any> | null;
    test_runs?: number;
    baseline?: Record<string, any>;
    regression?: {
      complete?: boolean | null;
      mode?: string | null;
      levels?: string[];
      new_failures?: number | null;
      status?: string | null;
      failure_reason?: string | null;
      executed_tests?: number;
    };
    closure?: Record<string, any>;
  };
  review: {
    executed?: boolean;
    approved?: boolean | null;
    summary?: string;
    risk?: string;
    issues?: ReviewIssue[];
    reviewed_patch_hash?: string;
    bound_to_candidate?: boolean | null;
    high_issues?: number | null;
    review_calls?: number;
    post_review_repair?: boolean;
    status?: string;
    skip_reason?: string[];
  };
  regression: Record<string, any>;
  usage: Usage;
  budget: Budget;
  attempts: Array<{
    iteration: number;
    patch_hash: string;
    patch_bytes: number;
    validation_passed: boolean | null;
    target_status: string | null;
    tests_failed: number | null;
    failure_signatures: string[];
    review_approved: boolean | null;
    diagnosis: boolean;
  }>;
  localization: Record<string, any>;
  plan: Record<string, any>;
  reproduction: Record<string, any>;
  failures: string[];
  unexecuted_stages: string[];
  artifacts: Record<string, any>;
  markdown: string;
  raw: Record<string, any>;
  readonly: boolean;
}

export interface IntegrityCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface Benchmark {
  bench_id: string;
  label: string;
  run_id: string;
  instance_id: string;
  repository: string;
  model: string;
  recorded_at: string | null;
  status: string;
  raw_status: string;
  ui_status: UiStatus;
  stop_reason: string | null;
  official: OfficialVerdict;
  internal: InternalVerification;
  usage: Usage;
  budget: Budget;
  tests: Report["tests"];
  review: Report["review"];
  harness: Record<string, any>;
  integrity: IntegrityCheck[];
  artifacts: Record<string, any>;
  note: string | null;
  readonly: boolean;
}

export interface HistoryItem {
  history_id: string;
  run_id: string;
  label: string;
  instance_id: string;
  repository: string;
  model: string;
  recorded_at: string | null;
  status: string;
  raw_status: string;
  ui_status: UiStatus;
  stop_reason: string | null;
  official_verdict: string | null;
  verification_level: string | null;
  event_count: number;
  patch_available: boolean;
  report_available: boolean;
  note: string | null;
}

export interface DemoRepository {
  name: string;
  label: string;
  description: string;
}

export interface Meta {
  product: string;
  version: string;
  allowed_repository_hosts: string[];
  demo_repositories: DemoRepository[];
  execution_modes: string[];
  live_llm_enabled: boolean;
  live_llm_reason: string | null;
  live_llm_reason_code: "disabled" | "requires_docker" | "no_models" | null;
  live_models: LiveModel[];
  default_model: { provider_id: string | null; model_id: string | null };
  settings_version: number;
  secure_storage_available: boolean;
  default_execution_mode: "offline_fake" | "docker" | "live";
  workspace_defaults: WorkspaceSettings;
  sandbox: { execution_backend: string; build_network: boolean; tools: string[] };
  default_budget: Budget;
  history_available: boolean;
  max_issue_chars: number;
  sse: { poll_seconds: number; heartbeat_seconds: number };
}

export interface CancelResponse {
  run_id: string;
  task_id: string;
  accepted: boolean;
  already_finished: boolean;
  cancel_requested: boolean;
  status: string;
  raw_status: string;
  ui_status: UiStatus;
  stop_reason: string | null;
  message: string;
}

export type StreamControl = {
  type: "stream_end";
  run_id: string;
  status: string;
  ui_status: UiStatus;
  stop_reason?: string | null;
  summary?: string;
};

export interface LiveModel {
  provider_id: string;
  provider_name: string;
  protocol: string;
  model_id: string;
  display_name: string;
  context_window: number | null;
  max_output_tokens: number | null;
  supports_tools: boolean;
  supports_json_mode: boolean;
  supports_thinking: boolean | null;
  price: ModelPriceView;
  currency: string;
  ready: boolean;
  blockers: string[];
  credential_reason: string;
  inference_verified: boolean;
  inference_state: TestState;
}

export interface ModelPriceView {
  currency: string;
  input_per_mtok: number | null;
  output_per_mtok: number | null;
  cached_input_per_mtok: number | null;
}

export interface CredentialView {
  kind: "none" | "env" | "vault";
  configured: boolean;
  available: boolean;
  reason: string;
  env_name: string | null;
  updated_at: string | null;
}

export type TestState = "ok" | "failed" | "stale" | "not_tested";
export type InferenceState = "verified" | "failed" | "stale" | "not_tested";

export interface ProviderTestSummary {
  state: TestState;
  checked_at: string | null;
  detail: string | null;
  latency_ms: number | null;
}

export interface InferenceStatus {
  state: InferenceState;
  checked_at: string | null;
  detail: string | null;
  latency_ms: number | null;
  model_echo: string | null;
  tool_calling: string;
}

export interface ProviderModel {
  model_id: string;
  display_name: string;
  context_window: number | null;
  max_output_tokens: number | null;
  supports_tools: boolean;
  supports_json_mode: boolean;
  supports_temperature: boolean;
  supports_thinking: boolean | null;
  price: ModelPriceView;
  source: "builtin" | "manual" | "discovered";
  source_note: string | null;
  favorite: boolean;
  routing_ready: boolean;
  blockers: string[];
  verification: {
    config_complete: boolean;
    missing: string[];
    credential: { available: boolean; reason: string };
    protocol: { available: boolean; reason: string };
    routing: { eligible: boolean; blockers: string[] };
    inference: InferenceStatus;
    tool_calling: string;
  };
}

export interface ProviderView {
  provider_id: string;
  name: string;
  protocol: string;
  base_url: string;
  enabled: boolean;
  custom: boolean;
  catalog_id: string | null;
  timeout_seconds: number;
  max_retries: number;
  max_concurrency: number;
  region: string | null;
  aws_profile: string | null;
  models: ProviderModel[];
  credential: CredentialView;
  protocol_supported: boolean;
  protocol_reason: string;
  verification: {
    routing_eligible: number;
    inference_verified: number;
    config_test: ProviderTestSummary;
    models_test: ProviderTestSummary;
  };
  editable: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface RoleRouteView {
  provider_id: string | null;
  model_id: string | null;
  reasoning: "inherit" | "disabled";
}

export interface GeneralSettings {
  language: "zh-CN" | "en-US" | "system";
  theme: "light" | "dark" | "system";
  default_execution_mode: "offline_fake" | "docker" | "live";
  show_reasoning: boolean;
  activity_density: "summary" | "normal" | "detailed";
}

export interface AgentSettings {
  default_provider_id: string | null;
  default_model_id: string | null;
  roles: Record<string, RoleRouteView>;
}

export interface ExecutionSettings {
  budget_currency: string;
  exchange_rates: Record<string, number>;
  max_iterations: number;
  max_test_runs: number;
  max_files_changed: number;
  max_lines_changed: number;
  max_tool_calls_per_iteration: number;
  max_review_calls: number;
  max_runtime_seconds: number;
  max_total_tokens: number;
  max_cost: number | null;
  max_parallel_tasks: number;
}

export interface WorkspaceSettings {
  default_repository_kind: "demo" | "https";
  default_repository: string;
}

export interface SettingsDocument {
  version: number;
  general: GeneralSettings;
  agents: AgentSettings;
  execution: ExecutionSettings;
  workspace: WorkspaceSettings;
  providers: ProviderView[];
  updated_at: string | null;
  secure_storage_available: boolean;
}

export interface CatalogTemplate {
  catalog_id: string;
  name: string;
  protocol: string;
  base_url: string;
  api_key_env: string | null;
  auth_kind: "api_key" | "aws";
  docs_url: string | null;
  notes: string;
  models: ProviderModel[];
  source: string;
  available: boolean;
  protocol_supported: boolean;
}

export interface ProviderTestCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface ProviderTestResult {
  kind: "config" | "models" | "inference";
  ok: boolean;
  checks: ProviderTestCheck[];
  models?: string[];
  latency_ms?: number;
  authenticated?: boolean;
  model_id?: string;
  model_echo?: string;
  finish_reason?: string | null;
  usage?: { input_tokens: number; output_tokens: number; cached_input_tokens: number };
  cost?: number;
  cost_known?: boolean;
  currency?: string;
  tool_calling?: string;
  checked_at?: string;
  fingerprint?: string;
}

export interface RuntimeFacts {
  product: string;
  version: string;
  git_commit: string | null;
  worker: {
    counts: Record<string, number>;
    queued: number;
    running: number;
    completed: number;
    stopped: number;
    failed: number;
    cancelled: number;
    uncertain: number;
    last_worker: string | null;
    lease_seconds: number;
    poll_seconds: number;
    execution_backend: string;
  };
  docker: { available: boolean; server_version: string | null; latency_ms: number; error: string | null };
  event_sink: { kind: string; path: string; status: string };
  checkpointer: { kind: string; scope: string; status: string };
  storage: { artifact_bytes: number; artifact_files: number; artifact_root: string };
  secure_storage_available: boolean;
  settings_version: number;
  providers: Array<{ kind: string; ok: boolean; checks: ProviderTestCheck[] }>;
  audit: Array<{ action: string; target: string; summary: string; created_at: string }>;
}

export interface CleanupResult {
  deleted: string[];
  skipped: Array<{ task_id: string; reason: string }>;
  settings_version: number;
}

export interface SettingsLogEntry {
  seq: number | null;
  run_id: string | null;
  type: string;
  node: string;
  status: string;
  summary: string;
  error: string;
  timestamp: string | null;
}
