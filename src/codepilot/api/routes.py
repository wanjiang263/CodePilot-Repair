"""Phase 13 HTTP routes: tasks, runs, SSE, patches, reports, cancel and replay."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from codepilot import __version__
from codepilot.api.database import ApiDatabase, TaskRow, parse_iso
from codepilot.api.history import HistoryCatalog, HistoryEntry
from codepilot.api.reporting import build_patch_view, build_report_view, load_json
from codepilot.api.schemas import (
    BudgetOverrides,
    BudgetView,
    CancelView,
    CreateTaskRequest,
    HistoryView,
    PatchView,
    ReportView,
    TaskDetailView,
    TaskListView,
    TaskView,
    UsageView,
)
from codepilot.api.security import (
    PathScrubber,
    RepositorySecurityError,
    validate_base_commit,
    validate_repository,
)
from codepilot.api.status import is_terminal, ui_status_for
from codepilot.config import Settings
from codepilot.schemas.budget import RepairBudget
from codepilot.settings.service import SettingsError, SettingsService

__all__ = ["ApiContext", "build_router", "task_view"]

_SSE_MEDIA_TYPE = "text/event-stream"


@dataclass(frozen=True, slots=True)
class ApiContext:
    settings: Settings
    database: ApiDatabase
    history: HistoryCatalog
    scrubber: PathScrubber
    content_scrubber: PathScrubber
    settings_service: SettingsService


def _context(request: Request) -> ApiContext:
    return cast(ApiContext, request.app.state.api_context)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _now() -> datetime:
    return datetime.now(UTC)


def task_view(context: ApiContext, row: TaskRow) -> TaskView:
    from codepilot.api.runtime_view import activity_view
    from codepilot.rsi import selection_view

    summary = context.scrubber.payload(row.summary)
    usage = row.usage
    frozen = _frozen_config(row)
    started = parse_iso(row.started_at)
    finished = parse_iso(row.finished_at)
    created = parse_iso(row.created_at)
    duration: float | None = None
    if started is not None:
        end = finished or (_now() if not row.terminal else started)
        duration = max(0.0, (end - started).total_seconds())
    runtime = activity_view(
        context.database.events_after(
            row.run_id, max(0, context.database.event_count(row.run_id) - 300)
        ),
        now=_now(),
        terminal=row.terminal,
    )
    runtime["policy_configuration"] = selection_view(runtime.get("policy_snapshot", {}))
    return TaskView(
        runtime={
            **runtime,
            "queued_seconds": max(0.0, ((started or _now()) - created).total_seconds())
            if created
            else None,
        },
        cancellation_state="confirmed"
        if row.status == "cancelled"
        else "requested"
        if row.cancel_requested_at
        else "not_requested",
        task_id=row.task_id,
        run_id=row.run_id,
        repository=row.repository_label,
        repository_kind=row.repository_kind,
        issue_preview=_preview(context.scrubber.text(row.issue), 220),
        status=row.status,
        raw_status=row.raw_status,
        ui_status=row.ui_status,
        stop_reason=row.stop_reason,
        created_at=parse_iso(row.created_at) or _now(),
        started_at=started,
        finished_at=finished,
        execution_mode=row.execution_mode,
        base_commit=row.resolved_base_commit or row.base_commit,
        attempts=row.attempts,
        recovery_state=row.recovery_state,
        cancel_requested=row.cancel_requested,
        usage=UsageView(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
            currency=str(usage.get("currency") or "CNY"),
            llm_calls=int(usage.get("llm_calls") or 0),
            cost_known=int(usage.get("cost_unknown_calls") or 0) == 0,
        ),
        verification_level=row.verification_level
        or (str(summary.get("verification_level")) if summary.get("verification_level") else None),
        patch_available=bool(row.patch_available),
        report_available=bool(row.report_available),
        events_count=context.database.event_count(row.run_id),
        duration_seconds=duration,
        simulated_llm=bool(summary.get("simulated_llm", frozen.get("mode") != "live")),
        simulated_sandbox=bool(summary.get("simulated_sandbox", row.execution_mode != "docker")),
        official_verdict=row.official_verdict,
        llm_mode=str(frozen.get("mode") or ("live" if row.execution_mode == "live" else "fake")),
        provider_id=frozen.get("default_provider_id"),
        model_id=frozen.get("default_model_id"),
        model_identity=_identity_from_frozen(frozen),
        config_hash=row.config_hash or None,
    )


def _frozen_config(row: TaskRow) -> dict[str, Any]:
    try:
        payload = json.loads(row.config_json or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _identity_from_frozen(frozen: dict[str, Any]) -> str | None:
    if frozen.get("mode") != "live":
        return "FakeLLM" if frozen else None
    roles = frozen.get("roles")
    if not isinstance(roles, dict) or not roles:
        return None
    parts = []
    for role, selection in sorted(roles.items()):
        if isinstance(selection, dict) and selection.get("model_id"):
            parts.append(f"{role}={selection['model_id']}")
    return "/".join(parts) or None


def _frozen_summary_from_row(row: TaskRow) -> dict[str, Any]:
    frozen = _frozen_config(row)
    if not frozen:
        return {}
    return {
        "llm_mode": frozen.get("mode"),
        "model_identity": _identity_from_frozen(frozen),
        "config_hash": row.config_hash or frozen.get("config_hash"),
        "provider_id": frozen.get("default_provider_id"),
        "model_id": frozen.get("default_model_id"),
    }


def _preview(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _task_detail(context: ApiContext, row: TaskRow) -> TaskDetailView:
    base = task_view(context, row)
    return TaskDetailView(
        **base.model_dump(),
        issue=context.scrubber.text(row.issue),
        budget=BudgetView.model_validate(context.scrubber.payload(row.budget)),
        summary=context.scrubber.payload(row.summary),
        error=context.scrubber.text(row.error) if row.error else None,
    )


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def ctx(request: Request) -> ApiContext:
        return _context(request)

    @router.get("/meta")
    def meta(request: Request) -> dict[str, Any]:
        context = ctx(request)
        demos = _demo_repositories(context.settings)
        service = context.settings_service
        document = service.document()
        live_models = service.available_models()
        live_reason: str | None = None
        live_reason_code: str | None = None
        if not context.settings.api_allow_live_llm:
            live_reason = "Live model execution is disabled by the server administrator."
            live_reason_code = "disabled"
        elif context.settings.api_execution_backend != "docker" and not (
            context.settings.api_live_allow_simulated_sandbox
        ):
            live_reason = "Live model execution requires the Docker sandbox backend."
            live_reason_code = "requires_docker"
        elif not any(item["ready"] for item in live_models):
            live_reason = "No provider/model is ready for live routing."
            live_reason_code = "no_models"
        live_enabled = live_reason is None
        default_budget = RepairBudget(
            max_iterations=document.execution.max_iterations,
            max_test_runs=document.execution.max_test_runs,
            max_files_changed=document.execution.max_files_changed,
            max_lines_changed=document.execution.max_lines_changed,
            max_tool_calls_per_iteration=document.execution.max_tool_calls_per_iteration,
            max_review_calls=document.execution.max_review_calls,
            max_runtime_seconds=document.execution.max_runtime_seconds,
            max_total_tokens=document.execution.max_total_tokens,
            max_cost=document.execution.max_cost,
            budget_currency=document.execution.budget_currency,
        )
        return {
            "product": "CodePilot-Repair",
            "version": __version__,
            "allowed_repository_hosts": sorted(context.settings.allowed_repository_hosts),
            "demo_repositories": demos,
            "execution_modes": [
                mode
                for mode in ("offline_fake", "docker", "live")
                if mode == "offline_fake"
                or (mode == "docker" and context.settings.api_execution_backend == "docker")
                or (mode == "live" and live_enabled)
            ],
            "live_llm_enabled": live_enabled,
            "live_llm_reason": live_reason,
            "live_llm_reason_code": live_reason_code,
            "live_models": live_models,
            "default_model": {
                "provider_id": document.agents.default_provider_id,
                "model_id": document.agents.default_model_id,
            },
            "settings_version": document.version,
            "secure_storage_available": service.vault.available,
            "default_execution_mode": (
                document.general.default_execution_mode
                if document.general.default_execution_mode
                in {
                    mode
                    for mode in ("offline_fake", "docker", "live")
                    if mode == "offline_fake"
                    or (mode == "live" and live_enabled)
                    or (mode == "docker" and context.settings.api_execution_backend == "docker")
                }
                else "offline_fake"
            ),
            "workspace_defaults": document.workspace.model_dump(mode="json"),
            "sandbox": {
                "execution_backend": context.settings.api_execution_backend,
                "build_network": False,
                "tools": [
                    "read_file",
                    "search_code",
                    "find_symbol",
                    "get_callers",
                    "get_callees",
                    "get_related_tests",
                    "edit_file",
                    "create_file",
                    "get_git_diff",
                    "run_tests",
                    "finish",
                ],
            },
            "default_budget": default_budget.model_dump(mode="json"),
            "history_available": bool(context.history.entries),
            "max_issue_chars": 20_000,
            "sse": {
                "poll_seconds": context.settings.api_sse_poll_seconds,
                "heartbeat_seconds": context.settings.api_sse_heartbeat_seconds,
            },
        }

    @router.get("/openapi.json")
    def openapi(request: Request) -> dict[str, Any]:
        return cast("dict[str, Any]", request.app.openapi())

    # ---------------------------------------------------------------- tasks
    @router.post("/tasks", status_code=status.HTTP_201_CREATED, response_model=TaskView)
    def create_task(request: Request, body: CreateTaskRequest) -> TaskView:
        context = ctx(request)
        try:
            repository = validate_repository(
                kind=body.repository.kind,
                value=body.repository.value,
                settings=context.settings,
            )
            base_commit = validate_base_commit(body.base_commit)
        except RepositorySecurityError as exc:
            raise _error(status.HTTP_400_BAD_REQUEST, "repository_rejected", str(exc)) from exc
        if body.execution_mode == "docker" and context.settings.api_execution_backend != "docker":
            raise _error(
                status.HTTP_400_BAD_REQUEST,
                "execution_mode_disabled",
                "Docker execution is disabled on this server; use offline_fake.",
            )
        execution = context.settings_service.document().execution
        budget = _budget(body.budget, execution)
        if body.execution_mode == "live":
            if not context.settings.api_allow_live_llm:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "live_llm_disabled",
                    "Live model execution is disabled on this server.",
                )
            if context.settings.api_execution_backend != "docker" and not (
                context.settings.api_live_allow_simulated_sandbox
            ):
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "live_requires_docker",
                    "Live model execution requires the Docker sandbox backend.",
                )
            if body.budget is None or body.budget.max_cost is None or body.budget.max_cost <= 0:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "live_cost_cap_required",
                    "A positive per-task cost hard cap is required for live runs.",
                )
            assert budget.max_cost is not None
            if budget.max_cost > context.settings.api_max_live_cost:
                raise _error(
                    status.HTTP_400_BAD_REQUEST,
                    "live_cost_cap_too_high",
                    f"The cost hard cap may not exceed {context.settings.api_max_live_cost}.",
                )
            if context.database.active_live_count() >= execution.max_parallel_tasks:
                raise _error(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "live_concurrency_limit",
                    "The configured number of live tasks is already running.",
                )
            selection = (
                (body.model.provider_id, body.model.model_id)
                if body.model is not None
                else (None, None)
            )
            try:
                snapshot = context.settings_service.build_live_snapshot(
                    provider_id=selection[0], model_id=selection[1]
                )
            except SettingsError as exc:
                raise _error(status.HTTP_400_BAD_REQUEST, exc.code, str(exc)) from exc
        else:
            snapshot = context.settings_service.fake_snapshot()
        task_id = "task_" + secrets.token_hex(10)
        run_id = "run_" + secrets.token_hex(10)
        row = context.database.create_task(
            task_id=task_id,
            run_id=run_id,
            repository_kind=repository.kind,
            repository_value=repository.value,
            repository_label=repository.label,
            base_commit=base_commit,
            issue=body.issue,
            mode="user",
            execution_mode=body.execution_mode,
            budget=budget.model_dump(mode="json"),
            config=snapshot.model_dump(mode="json"),
            config_hash=snapshot.config_hash,
        )
        return task_view(context, row)

    @router.get("/tasks", response_model=TaskListView)
    def list_tasks(
        request: Request,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
        status_filter: str | None = Query(default=None, alias="status"),
        repository: str | None = Query(default=None, max_length=200),
        q: str | None = Query(default=None, max_length=200),
    ) -> TaskListView:
        context = ctx(request)
        rows, total = context.database.list_tasks(
            limit=limit,
            offset=offset,
            status=status_filter,
            repository=repository,
            query=q,
        )
        return TaskListView(
            items=[task_view(context, row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/tasks/{task_id}", response_model=TaskDetailView)
    def get_task(request: Request, task_id: str) -> TaskDetailView:
        context = ctx(request)
        row = context.database.get_by_task(task_id)
        if row is None:
            raise _error(status.HTTP_404_NOT_FOUND, "task_not_found", "Task not found.")
        return _task_detail(context, row)

    # ---------------------------------------------------------------- events
    @router.get("/runs/{run_id}/events")
    async def run_events(
        request: Request,
        run_id: str,
        after_seq: int | None = Query(default=None, ge=0, le=10_000_000),
    ) -> EventSourceResponse:
        context = ctx(request)
        row = context.database.get_by_run(run_id)
        entry = None if row is not None else context.history.get_by_run(run_id)
        if row is None and entry is None:
            raise _error(status.HTTP_404_NOT_FOUND, "run_not_found", "Run not found.")
        header = request.headers.get("Last-Event-ID")
        cursor = after_seq
        if cursor is None and header is not None:
            try:
                cursor = max(0, int(header))
            except ValueError as exc:
                raise _error(
                    status.HTTP_400_BAD_REQUEST, "invalid_cursor", "Invalid cursor."
                ) from exc
        cursor = cursor or 0
        if entry is not None:
            return EventSourceResponse(
                _history_events(context, entry, cursor),
                ping=context.settings.api_sse_heartbeat_seconds,
                headers=_sse_headers(),
                media_type=_SSE_MEDIA_TYPE,
            )
        assert row is not None
        return EventSourceResponse(
            _live_events(context, request, run_id, cursor),
            ping=context.settings.api_sse_heartbeat_seconds,
            headers=_sse_headers(),
            media_type=_SSE_MEDIA_TYPE,
        )

    async def _history_events(context: ApiContext, entry: HistoryEntry, after_seq: int) -> Any:
        for event in context.history.events(entry):
            if int(event.get("seq") or 0) <= after_seq:
                continue
            yield _sse_event(event, "repair")
        yield _sse_event(
            {
                "run_id": entry.run_id,
                "task_id": entry.instance_id,
                "type": "stream_end",
                "seq": 0,
                "timestamp": _now().isoformat(),
                "status": "replay",
                "ui_status": ui_status_for(
                    str(entry.state.get("status") or "unknown"),
                    str(entry.state.get("stop_reason") or "") or None,
                ),
                "summary": "Historical artifact replay finished (read-only).",
                "extra": {"readonly": True},
            },
            "control",
        )

    async def _live_events(
        context: ApiContext, request: Request, run_id: str, after_seq: int
    ) -> Any:
        cursor = after_seq
        while True:
            if await request.is_disconnected():
                return
            events = context.database.events_after(run_id, cursor)
            for raw in events:
                seq = int(raw.get("seq") or 0)
                if seq <= cursor:
                    continue
                cursor = seq
                yield _sse_event(context.scrubber.payload(raw), "repair")
            row = context.database.get_by_run(run_id)
            if row is None:
                return
            if is_terminal(row.ui_status):
                remaining = context.database.events_after(run_id, cursor)
                if not remaining:
                    yield _sse_event(
                        {
                            "run_id": run_id,
                            "task_id": row.task_id,
                            "type": "stream_end",
                            "seq": cursor + 1,
                            "timestamp": _now().isoformat(),
                            "status": row.status,
                            "ui_status": row.ui_status,
                            "stop_reason": row.stop_reason,
                            "summary": "Run reached a terminal state.",
                            "extra": {"cancel_requested": row.cancel_requested},
                        },
                        "control",
                    )
                    return
                continue
            await asyncio.sleep(context.settings.api_sse_poll_seconds)

    # ---------------------------------------------------------------- patch/report
    @router.get("/runs/{run_id}/patch", response_model=PatchView)
    def run_patch(
        request: Request,
        run_id: str,
        file: str | None = Query(default=None, max_length=500),
    ) -> PatchView:
        context = ctx(request)
        row = context.database.get_by_run(run_id)
        entry = None if row is not None else context.history.get_by_run(run_id)
        if row is None and entry is None:
            raise _error(status.HTTP_404_NOT_FOUND, "run_not_found", "Run not found.")
        if entry is not None:
            run_dir = entry.run_dir
            state = entry.state
            report = entry.report
            task_id = str(state.get("task_id") or entry.instance_id)
            simulated = bool(report.get("simulated_llm") or report.get("simulated_sandbox"))
            checks = context.history.integrity(entry)
            check = next((item for item in checks if item.name == "patch-identity"), None)
            verified = bool(check and check.ok)
            note = check.detail if check else "No integrity record."
        else:
            assert row is not None
            run_dir = context.settings.artifacts_dir / row.artifact_rel
            state = load_json(run_dir / "state_final.json")
            report = load_json(run_dir / "report.json")
            task_id = row.task_id
            simulated = bool(report.get("simulated_llm") or report.get("simulated_sandbox"))
            expected = hashlib.sha256(
                str(state.get("current_patch") or "").encode("utf-8")
            ).hexdigest()
            try:
                patch_bytes = (run_dir / "patch_final.diff").read_bytes()
            except OSError:
                patch_bytes = b""
            actual = hashlib.sha256(patch_bytes).hexdigest() if patch_bytes else ""
            verified = bool(actual) and actual == expected
            note = (
                "Patch file matches state_final.current_patch."
                if verified
                else "Patch file is missing or does not match state_final.current_patch."
            )
            if patch_bytes and not verified:
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "patch_identity_mismatch",
                    note,
                )
        try:
            patch_text = (run_dir / "patch_final.diff").read_text(encoding="utf-8")
        except OSError:
            patch_text = ""
        if not patch_text.strip():
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "patch_not_available",
                "No final patch was produced for this run.",
            )
        if row is not None and report and report.get("run_id") not in (None, "", run_id):
            raise _error(
                status.HTTP_409_CONFLICT, "report_identity_mismatch", "Report run_id mismatch."
            )
        if state and state.get("run_id") not in (None, "", run_id):
            raise _error(
                status.HTTP_409_CONFLICT, "state_identity_mismatch", "State run_id mismatch."
            )
        view = build_patch_view(
            run_id=run_id,
            task_id=task_id,
            diff=patch_text,
            report=report,
            simulated=simulated,
            identity_verified=verified,
            identity_note=note,
        )
        return _select_patch(view, file=file, limit=context.settings.api_max_diff_bytes)

    @router.get("/runs/{run_id}/report", response_model=ReportView)
    def run_report(request: Request, run_id: str) -> ReportView:
        context = ctx(request)
        row = context.database.get_by_run(run_id)
        entry = None if row is not None else context.history.get_by_run(run_id)
        if row is None and entry is None:
            raise _error(status.HTTP_404_NOT_FOUND, "run_not_found", "Run not found.")
        if entry is not None:
            view = context.history.report(entry)
            return ReportView.model_validate(context.scrubber.payload(view.model_dump(mode="json")))
        assert row is not None
        run_dir = context.settings.artifacts_dir / row.artifact_rel
        state = load_json(run_dir / "state_final.json")
        report = load_json(run_dir / "report.json")
        if not report and not state:
            raise _error(
                status.HTTP_409_CONFLICT,
                "report_not_ready",
                "No report or final state exists yet.",
            )
        if report and report.get("run_id") not in (None, "", run_id):
            raise _error(
                status.HTTP_409_CONFLICT, "report_identity_mismatch", "Report run_id mismatch."
            )
        if state and state.get("run_id") not in (None, "", run_id):
            raise _error(
                status.HTTP_409_CONFLICT, "state_identity_mismatch", "State run_id mismatch."
            )
        payload = build_report_view(
            run_id=run_id,
            task_id=row.task_id,
            run_dir=run_dir,
            report=report,
            state=state,
            status=row.status,
            stop_reason=row.stop_reason,
            events_count=context.database.event_count(run_id),
            readonly=False,
            extra_summary=_frozen_summary_from_row(row),
        ).model_dump(mode="json")
        payload["markdown"] = context.content_scrubber.text(str(payload.get("markdown") or ""))
        markdown = payload["markdown"]
        sanitized = context.scrubber.payload({**payload, "markdown": ""})
        sanitized["markdown"] = markdown
        return ReportView.model_validate(sanitized)

    # ---------------------------------------------------------------- cancel
    @router.post("/runs/{run_id}/cancel", response_model=CancelView)
    def cancel_run(request: Request, run_id: str) -> CancelView:
        context = ctx(request)
        row = context.database.get_by_run(run_id)
        if row is None:
            if context.history.get_by_run(run_id) is not None:
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "readonly_replay",
                    "Historical artifacts are read-only and cannot be cancelled.",
                )
            raise _error(status.HTTP_404_NOT_FOUND, "run_not_found", "Run not found.")
        outcome = context.database.request_cancel(run_id)
        if outcome is None:  # pragma: no cover - row was read above
            raise _error(status.HTTP_404_NOT_FOUND, "run_not_found", "Run not found.")
        task = outcome.task
        return CancelView(
            run_id=run_id,
            task_id=task.task_id,
            accepted=outcome.accepted,
            already_finished=outcome.already_finished,
            cancel_requested=task.cancel_requested,
            status=task.status,
            raw_status=task.raw_status,
            ui_status=task.ui_status,
            stop_reason=task.stop_reason,
            message=outcome.message,
        )

    # ---------------------------------------------------------------- history
    @router.get("/history", response_model=list[HistoryView])
    def history_list(
        request: Request,
        q: str | None = Query(default=None, max_length=200),
    ) -> list[HistoryView]:
        context = ctx(request)
        needle = (q or "").strip().lower()
        return [
            context.history.history_item(entry)
            for entry in context.history.entries
            if not needle
            or needle in entry.label.lower()
            or needle in entry.instance_id.lower()
            or needle in entry.model.lower()
        ]

    @router.get("/benchmarks", response_model=list[HistoryView])
    def benchmark_list(
        request: Request, q: str | None = Query(default=None, max_length=200)
    ) -> list[HistoryView]:
        return history_list(request, q)

    @router.get("/benchmarks/{bench_id}")
    def benchmark_detail(request: Request, bench_id: str) -> Any:
        context = ctx(request)
        entry = context.history.get(bench_id)
        if entry is None:
            raise _error(status.HTTP_404_NOT_FOUND, "benchmark_not_found", "Benchmark not found.")
        view = context.history.benchmark(entry)
        return context.scrubber.payload(view.model_dump(mode="json"))

    return router


def _budget(overrides: BudgetOverrides | None, execution: Any | None = None) -> RepairBudget:
    defaults = RepairBudget().model_dump()
    if execution is not None:
        defaults.update(
            {
                "max_iterations": execution.max_iterations,
                "max_test_runs": execution.max_test_runs,
                "max_files_changed": execution.max_files_changed,
                "max_lines_changed": execution.max_lines_changed,
                "max_tool_calls_per_iteration": execution.max_tool_calls_per_iteration,
                "max_review_calls": execution.max_review_calls,
                "max_runtime_seconds": execution.max_runtime_seconds,
                "max_total_tokens": execution.max_total_tokens,
                "max_cost": execution.max_cost,
                "budget_currency": execution.budget_currency,
            }
        )
    if overrides is None:
        return RepairBudget(**defaults)
    supplied = overrides.model_dump(exclude_none=True)
    defaults.update(supplied)
    return RepairBudget(**defaults)


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def _sse_event(payload: dict[str, Any], event: str) -> dict[str, str]:
    return {
        "id": str(payload.get("seq") or 0),
        "event": event,
        "data": json.dumps(payload, ensure_ascii=False),
    }


def _select_patch(view: PatchView, *, file: str | None, limit: int) -> PatchView:
    if file:
        target = _safe_relative(file)
        match = next((item for item in view.files if item.path == target), None)
        if match is None:
            raise _error(
                status.HTTP_404_NOT_FOUND, "file_not_in_patch", "File is not in this patch."
            )
        view.files = [match]
        view.files_count = 1
        view.diff = match.diff
        view.diff_truncated = False
    encoded = view.diff.encode("utf-8")
    if len(encoded) <= limit:
        return view
    cut = view.diff.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    view.diff = cut + "\n[... diff truncated by the API; request a single file with ?file= ...]\n"
    view.diff_truncated = True
    per_file = max(512, limit // max(1, len(view.files)))
    for item in view.files:
        if len(item.diff.encode("utf-8")) <= per_file:
            continue
        item.diff = (
            item.diff.encode("utf-8")[:per_file].decode("utf-8", errors="ignore")
            + "\n[... file diff truncated ...]\n"
        )
    return view


def _safe_relative(path: str) -> str:
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or ".." in Path(normalized).parts
    ):
        raise _error(status.HTTP_400_BAD_REQUEST, "invalid_file", "Invalid patch file path.")
    return normalized


def _demo_repositories(settings: Settings) -> list[dict[str, str]]:
    root = settings.api_demo_root
    if not root.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        readme = path / "README.md"
        description = ""
        with contextlib.suppress(OSError, IndexError):
            description = readme.read_text(encoding="utf-8").splitlines()[0][:160]
        items.append({"name": path.name, "label": f"demo/{path.name}", "description": description})
    return items
