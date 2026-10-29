"""Stable HTTP contracts for the Phase 13 API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BenchmarkView",
    "BudgetOverrides",
    "BudgetView",
    "CancelView",
    "CreateTaskRequest",
    "EventUsageView",
    "EventView",
    "HistoryView",
    "IntegrityCheckView",
    "InternalVerificationView",
    "ModelSelection",
    "OfficialVerdictView",
    "PatchFileView",
    "PatchView",
    "ReportView",
    "RepositoryRef",
    "TaskDetailView",
    "TaskListView",
    "TaskView",
    "UsageView",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RepositoryRef(_StrictModel):
    kind: Literal["https", "demo"]
    value: str = Field(min_length=1, max_length=2048)


class BudgetOverrides(_StrictModel):
    max_iterations: int | None = Field(default=None, ge=1, le=10)
    max_test_runs: int | None = Field(default=None, ge=3, le=60)
    max_files_changed: int | None = Field(default=None, ge=1, le=20)
    max_lines_changed: int | None = Field(default=None, ge=1, le=2000)
    max_tool_calls_per_iteration: int | None = Field(default=None, ge=1, le=100)
    max_review_calls: int | None = Field(default=None, ge=1, le=5)
    max_runtime_seconds: int | None = Field(default=None, ge=30, le=7200)
    max_total_tokens: int | None = Field(default=None, ge=10_000, le=10_000_000)
    max_cost: float | None = Field(default=None, ge=0, le=1000)
    budget_currency: str | None = Field(default=None, min_length=2, max_length=8)


class ModelSelection(_StrictModel):
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=200)


class CreateTaskRequest(_StrictModel):
    repository: RepositoryRef
    issue: str = Field(min_length=10, max_length=20_000)
    base_commit: str = Field(default="HEAD", min_length=4, max_length=64)
    execution_mode: Literal["offline_fake", "docker", "live"] = "offline_fake"
    budget: BudgetOverrides | None = None
    model: ModelSelection | None = None


class UsageView(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    currency: str = "CNY"
    llm_calls: int = 0
    cost_known: bool = True


class EventUsageView(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    currency: str = "CNY"


class BudgetView(BaseModel):
    max_iterations: int | None = None
    max_test_runs: int | None = None
    max_files_changed: int | None = None
    max_lines_changed: int | None = None
    max_tool_calls_per_iteration: int | None = None
    max_review_calls: int | None = None
    max_runtime_seconds: int | None = None
    max_total_tokens: int | None = None
    max_cost: float | None = None
    budget_currency: str = "CNY"


class TaskView(BaseModel):
    runtime: dict[str, Any] = Field(default_factory=dict)
    cancellation_state: str = "not_requested"
    task_id: str
    run_id: str
    repository: str
    repository_kind: str
    issue_preview: str
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    execution_mode: str
    base_commit: str
    attempts: int = 0
    recovery_state: str | None = None
    cancel_requested: bool = False
    usage: UsageView = Field(default_factory=UsageView)
    verification_level: str | None = None
    patch_available: bool = False
    report_available: bool = False
    events_count: int = 0
    duration_seconds: float | None = None
    simulated_llm: bool = True
    simulated_sandbox: bool = True
    official_verdict: str | None = None
    llm_mode: str = "fake"
    provider_id: str | None = None
    model_id: str | None = None
    model_identity: str | None = None
    config_hash: str | None = None


class TaskDetailView(TaskView):
    issue: str
    budget: BudgetView = Field(default_factory=BudgetView)
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class TaskListView(BaseModel):
    items: list[TaskView]
    total: int
    limit: int
    offset: int


class EventView(BaseModel):
    run_id: str
    task_id: str
    seq: int
    type: str
    node: str
    timestamp: datetime | str | None = None
    status: str | None = None
    summary: str | None = None
    duration_ms: int | None = None
    usage: EventUsageView | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class PatchFileView(BaseModel):
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"
    diff: str = ""


class PatchView(BaseModel):
    run_id: str
    task_id: str
    sha256: str
    identity_sha256: str | None = None
    size_bytes: int
    files: list[PatchFileView]
    files_count: int
    additions: int
    deletions: int
    diff: str = ""
    diff_truncated: bool = False
    validation_passed: bool | None = None
    reviewer_approved: bool | None = None
    reviewed_patch_hash: str | None = None
    frozen_by_coding_stage: bool | None = None
    identity_verified: bool = True
    identity_note: str | None = None
    simulated: bool = False


class OfficialVerdictView(BaseModel):
    recorded: bool = False
    verdict: str | None = None
    resolved: bool | None = None
    status: str | None = None
    harness_status: str | None = None
    authoritative: bool = True
    source: str | None = None
    note: str | None = None


class InternalVerificationView(BaseModel):
    status: str | None = None
    raw_status: str | None = None
    stop_reason: str | None = None
    verification_level: str | None = None
    review_executed: bool | None = None
    review_bound_to_candidate: bool | None = None
    regression_complete: bool | None = None
    regression_new_failures: int | None = None
    target_tests_status: str | None = None
    unexecuted_stages: list[str] = Field(default_factory=list)
    closure: dict[str, Any] = Field(default_factory=dict)


class IntegrityCheckView(BaseModel):
    name: str
    ok: bool
    detail: str


class BenchmarkView(BaseModel):
    bench_id: str
    label: str
    run_id: str
    instance_id: str
    repository: str
    model: str
    recorded_at: str | None = None
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None = None
    official: OfficialVerdictView
    internal: InternalVerificationView
    usage: UsageView
    budget: BudgetView
    tests: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    harness: dict[str, Any] = Field(default_factory=dict)
    integrity: list[IntegrityCheckView] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None
    readonly: bool = True


class HistoryView(BaseModel):
    history_id: str
    run_id: str
    label: str
    instance_id: str
    repository: str
    model: str
    recorded_at: str | None = None
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None = None
    official_verdict: str | None = None
    verification_level: str | None = None
    event_count: int = 0
    patch_available: bool = False
    report_available: bool = False
    note: str | None = None


class ReportView(BaseModel):
    harness: dict[str, Any] = Field(default_factory=dict)
    run_id: str
    task_id: str
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None = None
    verification_level: str | None = None
    official: OfficialVerdictView = Field(default_factory=OfficialVerdictView)
    internal: InternalVerificationView = Field(default_factory=InternalVerificationView)
    issue: str = ""
    repository: str = ""
    base_commit: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)
    tests: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    regression: dict[str, Any] = Field(default_factory=dict)
    usage: UsageView = Field(default_factory=UsageView)
    budget: BudgetView = Field(default_factory=BudgetView)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    localization: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    reproduction: dict[str, Any] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    unexecuted_stages: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    markdown: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    readonly: bool = False


class CancelView(BaseModel):
    run_id: str
    task_id: str
    accepted: bool
    already_finished: bool = False
    cancel_requested: bool = False
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None = None
    message: str
