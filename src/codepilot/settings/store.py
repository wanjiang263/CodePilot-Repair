"""SQLite persistence for the typed settings document.

Optimistic concurrency is a single integer version on the document. Every
mutation supplies the version it read; a mismatch is a 409 rather than a silent
last-writer-wins overwrite. This matters because the UI can keep a settings tab
open while a worker/CLI changes routing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.settings.models import SettingsDocument

__all__ = ["SettingsStore", "VersionConflictError"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_documents (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_connection_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    model_id TEXT,
    kind TEXT NOT NULL,
    ok INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS settings_connection_tests_provider
    ON settings_connection_tests (provider_id, id DESC);
"""

_DOCUMENT = "settings"


class VersionConflictError(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"settings version conflict: expected {expected}, current {actual}")
        self.expected = expected
        self.actual = actual


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            now = _now()
            connection.execute(
                "INSERT OR IGNORE INTO settings_documents (name, version, payload_json, updated_at) "
                "VALUES (?, 0, ?, ?)",
                (_DOCUMENT, SettingsDocument().model_dump_json(), now),
            )
            connection.commit()

    def load(self) -> SettingsDocument:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version, payload_json FROM settings_documents WHERE name = ?",
                (_DOCUMENT,),
            ).fetchone()
        if row is None:  # pragma: no cover - initialize guarantees the row
            return SettingsDocument()
        payload = json.loads(str(row["payload_json"]))
        payload["version"] = int(row["version"])
        return SettingsDocument.model_validate(payload)

    def save(
        self,
        document: SettingsDocument,
        *,
        expected_version: int,
        action: str,
        target: str,
        summary: str,
    ) -> SettingsDocument:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT version FROM settings_documents WHERE name = ?",
                    (_DOCUMENT,),
                ).fetchone()
                actual = int(row["version"]) if row is not None else 0
                if actual != expected_version:
                    raise VersionConflictError(expected_version, actual)
                next_version = actual + 1
                now = _now()
                saved = document.model_copy(
                    update={"version": next_version, "updated_at": datetime.now(UTC)}
                )
                connection.execute(
                    "UPDATE settings_documents SET version = ?, payload_json = ?, updated_at = ? "
                    "WHERE name = ?",
                    (next_version, saved.model_dump_json(), now, _DOCUMENT),
                )
                connection.execute(
                    "INSERT INTO settings_audit (action, target, summary, created_at) "
                    "VALUES (?,?,?,?)",
                    (action, target, summary, now),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return saved

    def audit(self, *, limit: int = 40) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT action, target, summary, created_at FROM settings_audit "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_connection_test(
        self,
        *,
        provider_id: str,
        model_id: str | None,
        kind: str,
        ok: bool,
        fingerprint: str,
        detail: dict[str, Any],
    ) -> str:
        checked_at = _now()
        payload = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings_connection_tests "
                "(provider_id, model_id, kind, ok, fingerprint, detail_json, checked_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (provider_id, model_id, kind, 1 if ok else 0, fingerprint, payload, checked_at),
            )
            connection.commit()
        return checked_at

    def connection_tests(self, provider_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT provider_id, model_id, kind, ok, fingerprint, detail_json, checked_at "
                "FROM settings_connection_tests WHERE provider_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (provider_id, max(1, min(limit, 200))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["ok"] = bool(item["ok"])
            try:
                item["detail"] = json.loads(str(item.pop("detail_json")))
            except ValueError:
                item["detail"] = {}
            result.append(item)
        return result

    def delete_connection_tests(self, provider_id: str, model_id: str | None = None) -> None:
        with self.connect() as connection:
            if model_id is None:
                connection.execute(
                    "DELETE FROM settings_connection_tests WHERE provider_id = ?",
                    (provider_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM settings_connection_tests WHERE provider_id = ? AND model_id = ?",
                    (provider_id, model_id),
                )
            connection.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat()
