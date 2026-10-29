"""Independent background worker for the Phase 13 API.

The FastAPI process only writes a durable queue row. This module, run as
``codepilot worker``, claims that row and executes the **real M2 LangGraph**
through :class:`~codepilot.eval.assembly.InferenceAssembly`.

Defaults are deliberately conservative:

* one task at a time, single machine, SQLite only;
* FakeLLM + deterministic fake sandbox;
* no live provider, no Docker unless the task explicitly asks and the server
  allows the ``docker`` backend;
* checkpointed execution so a crashed worker can resume without replaying
  already-verified tool side effects.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from codepilot.api.database import ApiDatabase, EventArchive, TaskRow
from codepilot.api.demo import (
    DeterministicSandbox,
    build_demo_providers,
    demo_env_spec,
    materialize_demo_repo,
)
from codepilot.api.security import RepositorySecurityError, validate_repository
from codepilot.api.tasks import WebInferenceTask
from codepilot.artifacts import ArtifactStore
from codepilot.config import Settings
from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
from codepilot.observability.redaction import redact_text
from codepilot.observability.resources import ResourceCancelledError
from codepilot.repo.gitcmd import GitError, run_git
from codepilot.repo.workspace import (
    Workspace,
    WorkspaceError,
    WorkspaceManager,
    normalize_source,
    repository_id,
)
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.service import SettingsError, SettingsService, TaskModelSnapshot
from codepilot.settings.store import SettingsStore

__all__ = ["ApiWorker", "WorkerError"]

log = logging.getLogger("codepilot.api.worker")


class WorkerError(RuntimeError):
    """A task cannot be executed safely; the message is user-visible."""


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    repository: Path | str
    base_commit: str
    demo: bool


class ApiWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        database: ApiDatabase,
        worker_id: str | None = None,
        settings_service: SettingsService | None = None,
        client_factory: Any = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.worker_id = worker_id or f"worker_{secrets.token_hex(6)}"
        self.settings_service = settings_service or _default_settings_service(settings)
        self.client_factory = client_factory

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        recovered = self.database.recover_stale()
        if recovered:
            log.warning("api.worker.recovered", extra={"actions": recovered})
        self._write_health()
        while True:
            row = self.run_once()
            self._write_health()
            if row is None:
                time.sleep(self.settings.api_worker_poll_seconds)

    def _write_health(self) -> None:
        path = self.settings.resolved_worker_health_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_suffix(".tmp")
            staging.write_text(
                json.dumps(
                    {
                        "worker_id": self.worker_id,
                        "pid": os.getpid(),
                        "epoch": time.time(),
                        "queue": str(self.database.path),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            staging.replace(path)
        except OSError:
            log.warning("api.worker.health_write_failed", extra={"path": str(path)})

    def run_once(self) -> TaskRow | None:
        row = self.database.claim_next(
            worker_id=self.worker_id,
            lease_seconds=self.settings.api_worker_lease_seconds,
        )
        if row is None:
            return None
        self.execute(row)
        return row

    # --------------------------------------------------------------- execute
    def execute(self, row: TaskRow) -> None:
        run_id = row.run_id
        if self.database.is_cancel_requested(run_id):
            self._finish_cancelled_before_start(row)
            return
        heartbeat = _Heartbeat(
            database=self.database,
            run_id=run_id,
            worker_id=self.worker_id,
            lease_seconds=self.settings.api_worker_lease_seconds,
        )
        heartbeat.start()
        try:
            try:
                source = self._resolve_source(row)
                if source.base_commit:
                    self.database.mark_resolved_base(run_id, source.base_commit)
                outcome = self._assemble_and_run(row, source)
            except ResourceCancelledError as exc:
                self.database.append_event(
                    run_id=row.run_id,
                    task_id=row.task_id,
                    type="cancelled",
                    node="preparation",
                    status="cancelled",
                    summary=str(exc),
                )
                self.database.finish_task(
                    row.run_id,
                    status="cancelled",
                    stop_reason="cancelled",
                    usage=row.usage,
                    summary={**row.summary, "cancelled_while_waiting": True},
                    verification_level=row.verification_level or "cancelled",
                    patch_sha256=row.patch_sha256,
                    patch_available=bool(row.patch_available),
                    report_available=bool(row.report_available),
                )
            except RepositorySecurityError as exc:
                self._fail(row, "repository_rejected", str(exc), uncertain=False)
            except WorkerError as exc:
                self._fail(row, "worker_rejected", str(exc), uncertain=False)
            except WorkspaceError as exc:
                self._fail(row, "workspace_error", str(exc), uncertain=False)
            except GitError as exc:
                self._fail(row, "git_error", exc.render(), uncertain=False)
            except Exception as exc:
                log.exception("api.worker.failed", extra={"run_id": run_id})
                if self._recover_terminal(row):
                    return
                uncertain, detail = self._classify_exception(row, exc)
                self._fail(row, "worker_exception", detail, uncertain=uncertain)
            else:
                self._finish(row, outcome)
        except Exception:
            log.exception("api.worker.finish_failed", extra={"run_id": run_id})
        finally:
            heartbeat.stop()

    def _resolve_source(self, row: TaskRow) -> _ResolvedSource:
        validated = validate_repository(
            kind=row.repository_kind,
            value=row.repository_value,
            settings=self.settings,
        )
        if validated.kind == "demo":
            assert validated.local_path is not None
            repository, head = materialize_demo_repo(
                validated.local_path,
                self.settings.cache_dir,
            )
            base = row.base_commit
            if base.upper() == "HEAD":
                base = head
            return _ResolvedSource(repository=repository, base_commit=base, demo=True)
        url = validated.value
        base = row.base_commit
        if base.upper() == "HEAD":
            base = self._remote_head(url)
        return _ResolvedSource(repository=url, base_commit=base, demo=False)

    def _remote_head(self, url: str) -> str:
        result = run_git(
            [
                "-c",
                "http.followRedirects=false",
                "ls-remote",
                "--exit-code",
                url,
                "HEAD",
            ],
            timeout=60.0,
            check=False,
        )
        if result.returncode != 0:
            raise WorkerError(
                "无法解析远端 HEAD（可能网络不可达、重定向被拒绝或仓库不存在）："
                + redact_text(result.stderr.strip()[:500])
            )
        first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        sha = first.split()[0] if first else ""
        if len(sha) < 7:
            raise WorkerError("远端 HEAD 返回值不合法")
        return sha

    def _assemble_and_run(self, row: TaskRow, source: _ResolvedSource) -> Any:
        budget = RepairBudget(**row.budget)
        manager = WorkspaceManager(
            cache_root=self.settings.repo_cache_dir(),
            allow_local=source.demo,
            git_config={
                "http.followRedirects": "false",
                "http.lowSpeedLimit": "1",
                "http.lowSpeedTime": "60",
            },
        )
        # Reports keep a display identity, never a host path. The demo workspace
        # comes from the server-side factory, so the label is only metadata here.
        task_repository = row.repository_label if source.demo else str(source.repository)
        task = WebInferenceTask(
            task_id=row.task_id,
            issue=row.issue,
            base_commit=source.base_commit,
            repository=task_repository,
            env_spec=EnvSpec(install=(), env={"PYTHONPATH": "/workspace/src"}),
            mode="user",
        )
        bundle: ProviderBundle
        sandbox_factory: Any
        env_spec_factory: Any
        if row.execution_mode == "live":
            try:
                snapshot = TaskModelSnapshot.model_validate_json(row.config_json)
                config = self.settings_service.build_llm_config(snapshot)
            except (SettingsError, ValueError) as exc:
                raise WorkerError(str(exc)) from exc
            bundle = ProviderBundle.live(
                config,
                budget_currency=budget.budget_currency,
                client_factory=self.client_factory or self.settings_service.registry_client,
                bedrock_client_factory=self.settings_service.bedrock_client_factory,
            )
            if self.settings.api_execution_backend == "docker":

                def live_docker_factory(artifacts: ArtifactStore) -> Sandbox:
                    return DockerSandbox(
                        artifacts=artifacts,
                        allowed_workspace_root=self.settings.runs_dir(),
                        log_tail_bytes=self.settings.sandbox_log_tail_bytes,
                    )

                sandbox_factory = live_docker_factory
                env_spec_factory = None
            elif self.settings.api_live_allow_simulated_sandbox:

                def live_fake_factory(_artifacts: ArtifactStore) -> Sandbox:
                    return cast(Sandbox, DeterministicSandbox())

                sandbox_factory = live_fake_factory
                env_spec_factory = demo_env_spec
            else:
                raise WorkerError(
                    "live tasks require the Docker sandbox backend; simulated sandboxes are disabled"
                )
        elif row.execution_mode == "docker":
            bundle, _ = build_demo_providers()

            def docker_factory(artifacts: ArtifactStore) -> Sandbox:
                return DockerSandbox(
                    artifacts=artifacts,
                    allowed_workspace_root=self.settings.runs_dir(),
                    log_tail_bytes=self.settings.sandbox_log_tail_bytes,
                )

            sandbox_factory = docker_factory
            env_spec_factory = None
        else:
            bundle, _ = build_demo_providers()

            def fake_factory(_artifacts: ArtifactStore) -> Sandbox:
                return cast(Sandbox, DeterministicSandbox())

            sandbox_factory = fake_factory
            env_spec_factory = demo_env_spec

        def demo_workspace(artifacts: ArtifactStore) -> Workspace:
            return self._materialized_workspace(
                artifacts,
                Path(str(source.repository)),
                source.base_commit,
            )

        workspace_factory = demo_workspace if source.demo else None
        assembly = InferenceAssembly(
            task=task,
            workspace_manager=manager,
            work_root=self.settings.artifacts_dir,
            sandbox_factory=sandbox_factory,
            provider_bundle=bundle,
            budget=budget,
            with_reproducer=False,
            allow_new_tests=False,
            record_llm=False,
            checkpoint=True,
            env_spec_factory=env_spec_factory,
            enforce_base_only=False,
            allow_build_network=False,
            event_store=EventArchive(self.database),
            cancel_check=lambda: self.database.is_cancel_requested(row.run_id),
            workspace_factory=workspace_factory,
        )
        log.info(
            "api.worker.run",
            extra={
                "run_id": row.run_id,
                "task_id": row.task_id,
                "repository": row.repository_label,
                "execution_mode": row.execution_mode,
                "base_commit": source.base_commit,
            },
        )
        try:
            return assembly.run(run_id=row.run_id, seed=0)
        finally:
            bundle.close()

    def _materialized_workspace(
        self,
        artifacts: ArtifactStore,
        repository: Path,
        base_commit: str,
    ) -> Workspace:
        """Copy a freshly materialized demo repository into the run workspace.

        This avoids invoking git's local transport, which spawns an MSYS shell on
        Windows and is unavailable in some sandboxed hosts. All git operations
        afterwards (`status`, `diff`, `apply`) are built-ins and still run.
        """
        target = artifacts.run_dir / "workspace"
        if target.exists() and any(target.iterdir()):
            raise WorkspaceError(f"workspace target already exists and is not empty: {target}")
        try:
            shutil.copytree(repository, target)
            source = normalize_source(str(repository))
            workspace = Workspace(
                path=target,
                source=source,
                base_commit=base_commit,
                repository_id=repository_id(source, base_commit),
            )
            workspace.assert_clean()
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return workspace

    def _finish(self, row: TaskRow, outcome: Any) -> None:
        run_dir = self.settings.artifacts_dir / "runs" / row.run_id
        report_path = run_dir / "report.json"
        patch_path = run_dir / "patch_final.diff"
        usage = (
            outcome.usage.model_dump(mode="json") if hasattr(outcome.usage, "model_dump") else {}
        )
        summary = {
            "verification_level": outcome.verification_level,
            "review_executed": outcome.review_executed,
            "review_bound_to_candidate": outcome.review_bound_to_candidate,
            "regression_complete": outcome.unexecuted_stages == ()
            or "regression_incomplete" not in outcome.unexecuted_stages,
            "attempts": outcome.iterations,
            "runtime_seconds": outcome.runtime_seconds,
            "test_runs": outcome.test_runs,
            "simulated_llm": outcome.simulated_llm,
            "simulated_sandbox": outcome.sandbox_backend != "docker",
            "failure_reasons": list(outcome.failure_reasons),
            "unexecuted_stages": list(outcome.unexecuted_stages),
            "resumed_from_checkpoint": outcome.resumed_from_checkpoint,
            "patch_sha256": outcome.final_patch_sha256,
            **_frozen_summary(row),
        }
        patch_sha = outcome.final_patch_sha256 if patch_path.is_file() else None
        report_available = report_path.is_file()
        actual_status = outcome.status
        if (
            self.database.is_cancel_requested(row.run_id)
            and outcome.status not in {"cancelled"}
            and outcome.resumed_from_checkpoint is False
        ):
            # Cancellation can arrive after the graph's last safe boundary. Do not
            # rewrite a verification result; record the request separately.
            summary["cancel_requested_after_completion"] = True
        self.database.append_event(
            run_id=row.run_id,
            task_id=row.task_id,
            type="worker_finished",
            node="worker",
            status=actual_status,
            summary=f"Worker finished with status={actual_status}.",
            extra={
                "verification_level": outcome.verification_level,
                "simulated_llm": outcome.simulated_llm,
                "simulated_sandbox": summary["simulated_sandbox"],
            },
        )
        self.database.finish_task(
            row.run_id,
            status=actual_status,
            stop_reason=outcome.stop_reason,
            usage=usage,
            summary=summary,
            verification_level=outcome.verification_level,
            patch_sha256=patch_sha,
            patch_available=patch_path.is_file(),
            report_available=report_available,
            recovery_state="resumed" if outcome.resumed_from_checkpoint else None,
        )

    def _finish_cancelled_before_start(self, row: TaskRow) -> None:
        self.database.append_event(
            run_id=row.run_id,
            task_id=row.task_id,
            type="cancel_requested",
            node="worker",
            status="cancel_requested",
            summary="Cancellation was observed before any agent node started.",
        )
        self.database.append_event(
            run_id=row.run_id,
            task_id=row.task_id,
            type="cancelled",
            node="worker",
            status="cancelled",
            summary="Task cancelled without dispatching an agent node.",
        )
        self.database.finish_task(
            row.run_id,
            status="cancelled",
            stop_reason="cancelled",
            usage={},
            summary={"cancelled_before_start": True},
            verification_level="cancelled",
            patch_sha256=None,
            patch_available=False,
            report_available=False,
        )

    def _recover_terminal(self, row: TaskRow) -> bool:
        run_dir = self.settings.artifacts_dir / "runs" / row.run_id
        payload = _read_json(run_dir / "state_final.json")
        raw = str(payload.get("status") or "")
        try:
            status = RunStatus(raw)
        except ValueError:
            return False
        if status is RunStatus.RUNNING:
            return False
        report = _read_json(run_dir / "report.json")
        patch_path = run_dir / "patch_final.diff"
        patch_sha = (
            hashlib.sha256(patch_path.read_bytes()).hexdigest() if patch_path.is_file() else None
        )
        self.database.append_event(
            run_id=row.run_id,
            task_id=row.task_id,
            type="worker_recovered",
            node="worker",
            status=status.value,
            summary="A terminal state_final.json exists; recovered it without re-execution.",
            extra={"recovery_state": "recovered_terminal_state"},
        )
        self.database.finish_task(
            row.run_id,
            status=status.value,
            stop_reason=payload.get("stop_reason"),
            usage=_as_dict(payload.get("usage")),
            summary={
                "recovered": True,
                "verification_level": report.get("verification_level"),
                "failure_reasons": payload.get("failure_reasons") or [],
            },
            verification_level=report.get("verification_level"),
            patch_sha256=patch_sha,
            patch_available=patch_path.is_file(),
            report_available=(run_dir / "report.json").is_file(),
            recovery_state="recovered_terminal_state",
        )
        return True

    def _fail(self, row: TaskRow, kind: str, detail: str, *, uncertain: bool) -> None:
        message = redact_text(detail.strip())[:4000] or kind
        self.database.append_event(
            run_id=row.run_id,
            task_id=row.task_id,
            type="worker_failed",
            node="worker",
            status="uncertain" if uncertain else "failed",
            error=message,
            summary=f"Worker stopped: {kind}",
            extra={"kind": kind},
        )
        if uncertain:
            self.database.mark_uncertain(
                row.run_id,
                reason=(
                    "Worker error after external side effects may have started; "
                    f"the result is not re-executed. {message}"
                ),
            )
            return
        self.database.finish_task(
            row.run_id,
            status="failed",
            stop_reason="workflow_error",
            usage={},
            summary={"error_kind": kind, "error": message},
            verification_level="failed",
            patch_sha256=None,
            patch_available=False,
            report_available=False,
            error=message,
        )

    def _classify_exception(self, row: TaskRow, exc: Exception) -> tuple[bool, str]:
        run_dir = self.settings.artifacts_dir / "runs" / row.run_id
        state_path = run_dir / "state_final.json"
        if state_path.is_file():
            payload = _read_json(state_path)
            status = str(payload.get("status") or "")
            if status and status != "running":
                return False, (
                    f"{type(exc).__name__}: {exc}. A terminal state_final.json exists; "
                    "the run can be inspected without re-execution."
                )
        checkpoint = run_dir / "checkpoints.sqlite"
        events = run_dir / "events.jsonl"
        side_effects = checkpoint.is_file() or (events.is_file() and events.stat().st_size > 0)
        if side_effects:
            return True, f"{type(exc).__name__}: {exc}"
        return False, f"{type(exc).__name__}: {exc}"


class _Heartbeat:
    def __init__(
        self,
        *,
        database: ApiDatabase,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        self._database = database
        self._run_id = run_id
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"hb-{run_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        interval = max(5.0, self._lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                if not self._database.heartbeat(
                    self._run_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                ):
                    log.warning("api.worker.lease_lost", extra={"run_id": self._run_id})
            except Exception:  # pragma: no cover - heartbeat failure is logged, not fatal
                log.exception("api.worker.heartbeat_failed", extra={"run_id": self._run_id})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _as_dict(payload)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _frozen_summary(row: TaskRow) -> dict[str, Any]:
    try:
        snapshot = TaskModelSnapshot.model_validate_json(row.config_json)
    except (ValueError, TypeError):
        return {}
    return {
        "llm_mode": snapshot.mode,
        "model_identity": snapshot.model_identity,
        "config_hash": snapshot.config_hash,
        "provider_id": snapshot.default_provider_id,
        "model_id": snapshot.default_model_id,
    }


def _default_settings_service(settings: Settings) -> SettingsService:
    store = SettingsStore(settings.resolved_settings_path)
    vault = CredentialVault(
        settings.resolved_settings_credentials_dir,
        master_key=(
            settings.settings_master_key.get_secret_value()
            if settings.settings_master_key is not None
            else None
        ),
    )
    return SettingsService(store, vault)
