"""SQLite task queue, run identity and event archive for the API/Worker split.

Why SQLite instead of an in-process queue: the API must survive a restart without
losing completed work, and the Worker is a separate process by contract. The file
is the shared durable boundary. No Redis or distributed scheduler is required.

The event table is written by the Worker (through ``EventArchive``, compatible
with ``EventSink``) and read by the SSE endpoint. ``(run_id, seq)`` is the primary
key, so replay is exactly ``seq > after_seq`` with no duplicates and no gaps that
were actually persisted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codepilot.api.status import is_terminal, ui_status_for

__all__ = [
    "ApiDatabase",
    "EventArchive",
    "TaskRow",
    "now_iso",
    "parse_iso",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    repository_kind TEXT NOT NULL,
    repository_value TEXT NOT NULL,
    repository_label TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    resolved_base_commit TEXT NOT NULL DEFAULT '',
    issue TEXT NOT NULL,
    mode TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    config_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    worker_id TEXT,
    lease_expires_at TEXT,
    cancel_requested_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    resume_requested INTEGER NOT NULL DEFAULT 0,
    recovery_state TEXT,
    error TEXT,
    artifact_rel TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    raw_status TEXT NOT NULL,
    ui_status TEXT NOT NULL,
    stop_reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    verification_level TEXT,
    patch_sha256 TEXT,
    patch_available INTEGER NOT NULL DEFAULT 0,
    report_available INTEGER NOT NULL DEFAULT 0,
    official_verdict TEXT,
    replay_of TEXT,
    FOREIGN KEY(task_id) REFERENCES api_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY(run_id, seq),
    FOREIGN KEY(run_id) REFERENCES api_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_api_tasks_created ON api_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_api_tasks_status ON api_tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_api_runs_task ON api_runs(task_id);
"""

_JOIN = """
SELECT t.*,
       r.status AS status,
       r.raw_status AS raw_status,
       r.ui_status AS ui_status,
       r.stop_reason AS stop_reason,
       r.started_at AS started_at,
       r.finished_at AS finished_at,
       r.usage_json AS usage_json,
       r.summary_json AS summary_json,
       r.verification_level AS verification_level,
       r.patch_sha256 AS patch_sha256,
       r.patch_available AS patch_available,
       r.report_available AS report_available,
       r.official_verdict AS official_verdict
FROM api_tasks t JOIN api_runs r ON r.run_id = t.run_id
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class TaskRow:
    task_id: str
    run_id: str
    repository_kind: str
    repository_value: str
    repository_label: str
    base_commit: str
    resolved_base_commit: str
    issue: str
    mode: str
    execution_mode: str
    budget_json: str
    config_json: str
    config_hash: str
    created_at: str
    updated_at: str
    worker_id: str | None
    lease_expires_at: str | None
    cancel_requested_at: str | None
    attempts: int
    resume_requested: int
    recovery_state: str | None
    error: str | None
    artifact_rel: str
    status: str
    raw_status: str
    ui_status: str
    stop_reason: str | None
    started_at: str | None
    finished_at: str | None
    usage_json: str
    summary_json: str
    verification_level: str | None
    patch_sha256: str | None
    patch_available: int
    report_available: int
    official_verdict: str | None

    @property
    def budget(self) -> dict[str, Any]:
        return dict(json.loads(self.budget_json or "{}"))

    @property
    def usage(self) -> dict[str, Any]:
        return dict(json.loads(self.usage_json or "{}"))

    @property
    def summary(self) -> dict[str, Any]:
        payload = json.loads(self.summary_json or "{}")
        return dict(payload) if isinstance(payload, dict) else {}

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None

    @property
    def terminal(self) -> bool:
        return is_terminal(self.ui_status)


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    task: TaskRow
    accepted: bool
    already_finished: bool
    message: str


class ApiDatabase:
    def __init__(self, path: Path, *, artifacts_dir: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.artifacts_dir = Path(artifacts_dir).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    # ------------------------------------------------------------- plumbing
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            self._migrate(connection)
            connection.commit()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(api_tasks)").fetchall()
        }
        if "config_json" not in columns:
            connection.execute(
                "ALTER TABLE api_tasks ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "config_hash" not in columns:
            connection.execute(
                "ALTER TABLE api_tasks ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''"
            )

    # --------------------------------------------------------------- reads
    def _row(self, connection: sqlite3.Connection, run_id: str) -> TaskRow | None:
        row = connection.execute(f"{_JOIN} WHERE r.run_id = ?", (run_id,)).fetchone()
        return TaskRow(**dict(row)) if row is not None else None

    def _row_by_task(self, connection: sqlite3.Connection, task_id: str) -> TaskRow | None:
        row = connection.execute(f"{_JOIN} WHERE t.task_id = ?", (task_id,)).fetchone()
        return TaskRow(**dict(row)) if row is not None else None

    def get_by_task(self, task_id: str) -> TaskRow | None:
        with self.connect() as connection:
            return self._row_by_task(connection, task_id)

    def get_by_run(self, run_id: str) -> TaskRow | None:
        with self.connect() as connection:
            return self._row(connection, run_id)

    def queue_stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT r.ui_status AS ui_status, COUNT(*) AS count FROM api_runs r "
                "GROUP BY r.ui_status"
            ).fetchall()
            last_worker = connection.execute(
                "SELECT worker_id FROM api_tasks WHERE worker_id IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        counts = {str(row["ui_status"]): int(row["count"]) for row in rows}
        return {
            "counts": counts,
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "completed": counts.get("completed", 0),
            "stopped": counts.get("stopped", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "uncertain": counts.get("uncertain", 0),
            "last_worker": str(last_worker["worker_id"]) if last_worker is not None else None,
        }

    def active_live_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM api_tasks t JOIN api_runs r ON r.run_id = t.run_id "
                "WHERE t.execution_mode = 'live' AND r.ui_status IN ('queued', 'running')"
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def delete_finished_task(self, task_id: str) -> TaskRow:
        """Delete one finished user task and its queue rows.

        Historical benchmark packages live outside this database; this method
        only sees API-created tasks and never touches the read-only catalog.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_by_task(connection, task_id)
                if row is None:
                    raise KeyError("task not found")
                if not is_terminal(row.ui_status):
                    raise ValueError("only finished tasks can be deleted")
                if row.mode != "user":
                    raise ValueError("only user-created tasks can be deleted")
                connection.execute("DELETE FROM api_events WHERE run_id = ?", (row.run_id,))
                connection.execute("DELETE FROM api_runs WHERE run_id = ?", (row.run_id,))
                connection.execute("DELETE FROM api_tasks WHERE task_id = ?", (task_id,))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return row

    def list_tasks(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        repository: str | None = None,
        query: str | None = None,
    ) -> tuple[list[TaskRow], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status in {
                "queued",
                "running",
                "completed",
                "stopped",
                "failed",
                "cancelled",
                "error",
                "uncertain",
            }:
                clauses.append("r.ui_status = ?")
            else:
                clauses.append("r.raw_status = ?")
            params.append(status)
        if repository:
            clauses.append("t.repository_label LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(repository)}%")
        if query:
            clauses.append("(t.issue LIKE ? ESCAPE '\\' OR t.repository_label LIKE ? ESCAPE '\\')")
            escaped = f"%{_escape_like(query)}%"
            params.extend([escaped, escaped])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM api_tasks t JOIN api_runs r ON r.run_id=t.run_id{where}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{_JOIN}{where} ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [TaskRow(**dict(row)) for row in rows], total

    def previous_for_repository(self, repository_label: str, *, limit: int = 5) -> list[TaskRow]:
        with self.connect() as connection:
            rows = connection.execute(
                f"{_JOIN} WHERE t.repository_label = ? ORDER BY t.created_at DESC LIMIT ?",
                (repository_label, limit),
            ).fetchall()
        return [TaskRow(**dict(row)) for row in rows]

    def event_count(self, run_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM api_events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def last_event(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM api_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def events_after(
        self, run_id: str, after_seq: int, *, limit: int = 1000
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM api_events WHERE run_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (run_id, after_seq, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent_events(self, *, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM api_events ORDER BY rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # -------------------------------------------------------------- create
    def create_task(
        self,
        *,
        task_id: str,
        run_id: str,
        repository_kind: str,
        repository_value: str,
        repository_label: str,
        base_commit: str,
        issue: str,
        mode: str,
        execution_mode: str,
        budget: dict[str, Any],
        config: dict[str, Any] | None = None,
        config_hash: str = "",
    ) -> TaskRow:
        timestamp = now_iso()
        artifact_rel = f"runs/{run_id}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO api_tasks (task_id, run_id, repository_kind, repository_value, "
                    "repository_label, base_commit, issue, mode, execution_mode, budget_json, "
                    "config_json, config_hash, created_at, updated_at, artifact_rel) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        run_id,
                        repository_kind,
                        repository_value,
                        repository_label,
                        base_commit,
                        issue,
                        mode,
                        execution_mode,
                        json.dumps(budget, ensure_ascii=False, sort_keys=True),
                        json.dumps(config or {}, ensure_ascii=False, sort_keys=True),
                        config_hash,
                        timestamp,
                        timestamp,
                        artifact_rel,
                    ),
                )
                connection.execute(
                    "INSERT INTO api_runs (run_id, task_id, status, raw_status, ui_status) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, task_id, "queued", "queued", "queued"),
                )
                self._append_event_tx(
                    connection,
                    run_id=run_id,
                    task_id=task_id,
                    type="task_queued",
                    node="api",
                    status="queued",
                    summary="Task accepted and persisted; waiting for a worker.",
                    extra={"execution_mode": execution_mode, "repository": repository_label},
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            row = self._row(connection, run_id)
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("task insert did not become readable")
        return row

    # --------------------------------------------------------------- queue
    def claim_next(self, *, worker_id: str, lease_seconds: int) -> TaskRow | None:
        timestamp = now_iso()
        lease = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                selected = connection.execute(
                    "SELECT t.task_id, t.run_id FROM api_tasks t "
                    "JOIN api_runs r ON r.run_id = t.run_id "
                    "WHERE r.status = 'queued' ORDER BY t.created_at ASC LIMIT 1"
                ).fetchone()
                if selected is None:
                    connection.rollback()
                    return None
                updated = connection.execute(
                    "UPDATE api_tasks SET worker_id = ?, lease_expires_at = ?, attempts = attempts + 1, "
                    "updated_at = ? WHERE task_id = ?",
                    (worker_id, lease, timestamp, selected["task_id"]),
                )
                if updated.rowcount != 1:  # pragma: no cover - single-worker invariant
                    connection.rollback()
                    return None
                connection.execute(
                    "UPDATE api_runs SET status = 'running', raw_status = 'running', "
                    "ui_status = 'running', started_at = COALESCE(started_at, ?) WHERE run_id = ?",
                    (timestamp, selected["run_id"]),
                )
                self._append_event_tx(
                    connection,
                    run_id=selected["run_id"],
                    task_id=selected["task_id"],
                    type="worker_started",
                    node="worker",
                    status="running",
                    summary=f"Worker {worker_id} claimed the task.",
                    extra={"worker_id": worker_id, "attempt": 0},
                )
                connection.commit()
                row = self._row(connection, selected["run_id"])
            except BaseException:
                connection.rollback()
                raise
        return row

    def heartbeat(self, run_id: str, *, worker_id: str, lease_seconds: int) -> bool:
        lease = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE api_tasks SET lease_expires_at = ?, updated_at = ? "
                "WHERE run_id = ? AND worker_id = ? "
                "AND run_id IN (SELECT run_id FROM api_runs WHERE status = 'running')",
                (lease, now_iso(), run_id, worker_id),
            )
            connection.commit()
        return updated.rowcount == 1

    def mark_resolved_base(self, run_id: str, base_commit: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE api_tasks SET resolved_base_commit = ?, updated_at = ? WHERE run_id = ?",
                (base_commit, now_iso(), run_id),
            )
            connection.commit()

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested_at FROM api_tasks WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row and row[0])

    def request_cancel(self, run_id: str) -> CancelOutcome | None:
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(connection, run_id)
                if row is None:
                    connection.rollback()
                    return None
                if row.terminal:
                    connection.rollback()
                    return CancelOutcome(
                        task=row,
                        accepted=True,
                        already_finished=True,
                        message="Run already finished; nothing was changed.",
                    )
                if row.cancel_requested:
                    connection.rollback()
                    return CancelOutcome(
                        task=row,
                        accepted=True,
                        already_finished=False,
                        message="Cancellation was already requested.",
                    )
                if row.status == "queued":
                    self._set_status_tx(
                        connection,
                        task_id=row.task_id,
                        run_id=row.run_id,
                        status="cancelled",
                        stop_reason="cancelled",
                        finished=True,
                        error=None,
                    )
                    connection.execute(
                        "UPDATE api_tasks SET cancel_requested_at = ? WHERE task_id = ?",
                        (timestamp, row.task_id),
                    )
                    connection.execute(
                        "UPDATE api_runs SET verification_level = 'cancelled', summary_json = ? "
                        "WHERE run_id = ?",
                        (
                            json.dumps({"cancelled_before_start": True}, ensure_ascii=False),
                            row.run_id,
                        ),
                    )
                    self._append_event_tx(
                        connection,
                        run_id=row.run_id,
                        task_id=row.task_id,
                        type="cancelled",
                        node="api",
                        status="cancelled",
                        summary="Queued task cancelled before a worker started it.",
                        extra={"reason": "cancelled"},
                    )
                    connection.commit()
                    updated = self._row(connection, run_id)
                    assert updated is not None
                    return CancelOutcome(
                        task=updated,
                        accepted=True,
                        already_finished=False,
                        message="Queued task cancelled; no agent execution started.",
                    )
                # Running runs have exactly one event writer: the worker's
                # EventSink. Appending a lifecycle row here would race with its
                # local sequence counter and can violate (run_id, seq). The API
                # records the durable cancellation flag; the worker writes the
                # observable stop/cancel events at its next safe boundary.
                connection.execute(
                    "UPDATE api_tasks SET cancel_requested_at = ?, updated_at = ? WHERE task_id = ?",
                    (timestamp, timestamp, row.task_id),
                )
                connection.commit()
                updated = self._row(connection, run_id)
            except BaseException:
                connection.rollback()
                raise
        assert updated is not None
        return CancelOutcome(
            task=updated,
            accepted=True,
            already_finished=False,
            message="Cancellation requested. Side effects already performed are preserved.",
        )

    # -------------------------------------------------------------- finish
    def finish_task(
        self,
        run_id: str,
        *,
        status: str,
        stop_reason: str | None,
        usage: dict[str, Any],
        summary: dict[str, Any],
        verification_level: str | None,
        patch_sha256: str | None,
        patch_available: bool,
        report_available: bool,
        error: str | None = None,
        recovery_state: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(connection, run_id)
                if row is None:
                    connection.rollback()
                    return
                self._set_status_tx(
                    connection,
                    task_id=row.task_id,
                    run_id=row.run_id,
                    status=status,
                    stop_reason=stop_reason,
                    finished=True,
                    error=error,
                )
                connection.execute(
                    "UPDATE api_runs SET usage_json = ?, summary_json = ?, verification_level = ?, "
                    "patch_sha256 = ?, patch_available = ?, report_available = ? WHERE run_id = ?",
                    (
                        json.dumps(usage, ensure_ascii=False),
                        json.dumps(summary, ensure_ascii=False),
                        verification_level,
                        patch_sha256,
                        int(patch_available),
                        int(report_available),
                        run_id,
                    ),
                )
                if recovery_state is not None:
                    connection.execute(
                        "UPDATE api_tasks SET recovery_state = ? WHERE task_id = ?",
                        (recovery_state, row.task_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def mark_uncertain(self, run_id: str, *, reason: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(connection, run_id)
                if row is None:
                    connection.rollback()
                    return
                self._set_status_tx(
                    connection,
                    task_id=row.task_id,
                    run_id=row.run_id,
                    status="uncertain",
                    stop_reason="worker_lost",
                    finished=True,
                    error=reason,
                )
                connection.execute(
                    "UPDATE api_tasks SET recovery_state = 'result_unknown' WHERE task_id = ?",
                    (row.task_id,),
                )
                self._append_event_tx(
                    connection,
                    run_id=run_id,
                    task_id=row.task_id,
                    type="result_uncertain",
                    node="worker",
                    status="uncertain",
                    summary=reason,
                    extra={"recovery_state": "result_unknown"},
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _set_status_tx(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        run_id: str,
        status: str,
        stop_reason: str | None,
        finished: bool,
        error: str | None,
    ) -> None:
        timestamp = now_iso()
        ui_status = ui_status_for(status, stop_reason)
        connection.execute(
            "UPDATE api_runs SET status = ?, raw_status = ?, ui_status = ?, stop_reason = ?, "
            "finished_at = CASE WHEN ? THEN ? ELSE finished_at END WHERE run_id = ?",
            (status, status, ui_status, stop_reason, int(finished), timestamp, run_id),
        )
        connection.execute(
            "UPDATE api_tasks SET updated_at = ?, error = ?, "
            "lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END WHERE task_id = ?",
            (timestamp, error, int(finished), task_id),
        )

    # -------------------------------------------------------------- events
    def append_event(
        self,
        *,
        run_id: str,
        task_id: str,
        type: str,
        node: str,
        status: str | None = None,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        duration_ms: int | None = None,
        timestamp: datetime | str | None = None,
    ) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                seq = self._append_event_tx(
                    connection,
                    run_id=run_id,
                    task_id=task_id,
                    type=type,
                    node=node,
                    status=status,
                    summary=summary,
                    extra=extra,
                    error=error,
                    usage=usage,
                    tool_calls=tool_calls,
                    duration_ms=duration_ms,
                    timestamp=timestamp,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return seq

    def next_seq(self, run_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM api_events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0])

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        task_id: str,
        type: str,
        node: str,
        status: str | None = None,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        duration_ms: int | None = None,
        timestamp: datetime | str | None = None,
        seq: int | None = None,
    ) -> int:
        next_value = seq
        if next_value is None:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM api_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            next_value = int(row[0])
        stamp = (
            timestamp
            if isinstance(timestamp, str)
            else (timestamp or datetime.now(UTC)).isoformat()
        )
        payload: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "seq": next_value,
            "type": type,
            "node": node,
            "timestamp": stamp,
            "status": status,
            "summary": summary,
            "duration_ms": duration_ms,
            "usage": usage,
            "tool_calls": list(tool_calls or []),
            "error": error,
            "extra": dict(extra or {}),
        }
        connection.execute(
            "INSERT INTO api_events (run_id, seq, payload_json, timestamp) VALUES (?,?,?,?)",
            (run_id, next_value, json.dumps(payload, ensure_ascii=False), stamp),
        )
        return next_value

    def _append_event(
        self,
        *,
        run_id: str,
        node: str,
        type: str,
        payload: dict[str, Any],
        seq: int,
        timestamp: datetime,
    ) -> None:
        """EventSink adapter path: the payload is already redacted and shaped."""
        normalized = dict(payload)
        normalized.update({"run_id": run_id, "seq": seq, "node": node, "type": type})
        normalized.setdefault("timestamp", timestamp.isoformat())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO api_events (run_id, seq, payload_json, timestamp) VALUES (?,?,?,?)",
                    (
                        run_id,
                        seq,
                        json.dumps(normalized, ensure_ascii=False),
                        timestamp.isoformat(),
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    # ------------------------------------------------------------- recovery
    def recover_stale(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Classify tasks whose worker lease expired.

        Three outcomes, deliberately distinct:

        * terminal evidence on disk -> recover the real status, never re-execute;
        * a checkpoint + workspace -> queue for checkpoint resume;
        * anything else -> ``uncertain`` because external side effects cannot be
          proven absent. Re-running could duplicate tool side effects and cost.
        """
        current = now or datetime.now(UTC)
        actions: list[dict[str, Any]] = []
        with self.connect() as connection:
            rows = connection.execute(
                f"{_JOIN} WHERE r.status = 'running' AND t.lease_expires_at IS NOT NULL "
                "AND t.lease_expires_at < ?",
                (current.isoformat(),),
            ).fetchall()
        for raw in rows:
            row = TaskRow(**dict(raw))
            actions.append(self._recover_one(row))
        return actions

    def _recover_one(self, row: TaskRow) -> dict[str, Any]:
        run_dir = self.artifacts_dir / row.artifact_rel
        state_path = run_dir / "state_final.json"
        checkpoint = run_dir / "checkpoints.sqlite"
        workspace = run_dir / "workspace"
        if row.cancel_requested:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._set_status_tx(
                    connection,
                    task_id=row.task_id,
                    run_id=row.run_id,
                    status="cancelled",
                    stop_reason="cancelled",
                    finished=True,
                    error=None,
                )
                connection.execute(
                    "UPDATE api_tasks SET recovery_state = 'cancelled_on_recovery' WHERE task_id = ?",
                    (row.task_id,),
                )
                connection.commit()
            return {"run_id": row.run_id, "outcome": "cancelled"}
        if state_path.is_file():
            payload = _read_json(state_path)
            state_status = str(payload.get("status") or "error")
            stop_reason = payload.get("stop_reason")
            usage = payload.get("usage") or {}
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._set_status_tx(
                    connection,
                    task_id=row.task_id,
                    run_id=row.run_id,
                    status=state_status,
                    stop_reason=str(stop_reason) if stop_reason else None,
                    finished=True,
                    error=None,
                )
                connection.execute(
                    "UPDATE api_runs SET usage_json = ?, summary_json = ?, report_available = ?, "
                    "patch_available = ? WHERE run_id = ?",
                    (
                        json.dumps(usage, ensure_ascii=False),
                        json.dumps({"recovered": True}, ensure_ascii=False),
                        int((run_dir / "report.json").is_file()),
                        int((run_dir / "patch_final.diff").is_file()),
                        row.run_id,
                    ),
                )
                connection.execute(
                    "UPDATE api_tasks SET recovery_state = 'recovered_terminal_state' WHERE task_id = ?",
                    (row.task_id,),
                )
                connection.commit()
            return {"run_id": row.run_id, "outcome": "recovered", "status": state_status}
        if checkpoint.is_file() and workspace.is_dir():
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                timestamp = now_iso()
                connection.execute(
                    "UPDATE api_runs SET status = 'queued', raw_status = 'queued', ui_status = 'queued', "
                    "stop_reason = NULL, finished_at = NULL WHERE run_id = ?",
                    (row.run_id,),
                )
                connection.execute(
                    "UPDATE api_tasks SET worker_id = NULL, lease_expires_at = NULL, resume_requested = 1, "
                    "recovery_state = 'pending_resume', updated_at = ? WHERE task_id = ?",
                    (timestamp, row.task_id),
                )
                self._append_event_tx(
                    connection,
                    run_id=row.run_id,
                    task_id=row.task_id,
                    type="recovery_queued",
                    node="worker",
                    status="queued",
                    summary="Worker lease expired; checkpoint and workspace exist, queued for resume.",
                    extra={"recovery_state": "pending_resume"},
                )
                connection.commit()
            return {"run_id": row.run_id, "outcome": "pending_resume"}
        reason = (
            "Worker lease expired and neither a terminal state nor a resumable checkpoint/workspace "
            "was found. External side effects are unknown; the run was not re-executed."
        )
        self.mark_uncertain(row.run_id, reason=reason)
        return {"run_id": row.run_id, "outcome": "uncertain"}


class EventArchive:
    """``EventSink`` store protocol backed by the API SQLite database."""

    def __init__(self, database: ApiDatabase) -> None:
        self._database = database

    def next_seq(self, run_id: str) -> int:
        return self._database.next_seq(run_id)

    def append(
        self,
        *,
        run_id: str,
        node: str,
        type: str,
        payload: dict[str, Any] | None = None,
        seq: int | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self._database._append_event(
            run_id=run_id,
            node=node,
            type=type,
            payload=dict(payload or {}),
            seq=seq or self._database.next_seq(run_id),
            timestamp=timestamp or datetime.now(UTC),
        )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
