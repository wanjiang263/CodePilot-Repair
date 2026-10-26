"""Explicit, content-checked reuse of a base repository index."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from codepilot.repo.scanner import RepositoryScanner
from codepilot.repo.workspace import Workspace


def reuse_index(source: Path, destination: Path, workspace: Workspace, expected_sha: str) -> None:
    if destination.exists():
        raise ValueError("index destination already exists")
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha:
        raise ValueError("index snapshot hash mismatch")
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("index snapshot is corrupt")
        identities = connection.execute("SELECT id, url, base_commit FROM repositories").fetchall()
        if identities != [
            (workspace.repository_id, workspace.source.normalized, workspace.base_commit)
        ]:
            raise ValueError("index snapshot repository identity mismatch")
        files = dict(
            connection.execute(
                "SELECT path, sha256 FROM files WHERE repository_id=?", (workspace.repository_id,)
            )
        )
        scanned = RepositoryScanner().scan(workspace.path)
        if scanned.unreadable or not files or files != {f.path: f.sha256 for f in scanned.files}:
            raise ValueError("index snapshot content mismatch")
        with sqlite3.connect(destination) as target:
            connection.backup(target)
