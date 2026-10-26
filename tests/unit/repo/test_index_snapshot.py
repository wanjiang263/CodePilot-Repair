from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from codepilot.repo.index_snapshot import reuse_index
from codepilot.repo.scanner import RepositoryScanner
from codepilot.repo.workspace import Workspace, normalize_source


@pytest.mark.parametrize("change", [None, "content", "identity", "hash"])
def test_index_snapshot_requires_matching_identity_and_all_content(
    tmp_path: Path, change: str | None
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "core.py").write_text("answer = 42\n")
    ws = Workspace(root, normalize_source("https://github.com/owner/repo"), "a" * 40, "repo_id")
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE repositories (id, url, base_commit)")
        db.execute("CREATE TABLE files (repository_id, path, sha256)")
        db.execute(
            "INSERT INTO repositories VALUES (?,?,?)",
            (
                ws.repository_id,
                ws.source.normalized,
                ws.base_commit if change != "identity" else "wrong",
            ),
        )
        db.executemany(
            "INSERT INTO files VALUES (?,?,?)",
            [(ws.repository_id, f.path, f.sha256) for f in RepositoryScanner().scan(root).files],
        )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if change == "content":
        (root / "core.py").write_text("answer = 0\n")
    target = tmp_path / "target.sqlite3"
    if change:
        with pytest.raises(ValueError, match="mismatch"):
            reuse_index(source, target, ws, "wrong" if change == "hash" else digest)
        assert not target.exists()
    else:
        reuse_index(source, target, ws, digest)
        with sqlite3.connect(target) as db:
            assert db.execute("SELECT COUNT(*) FROM files").fetchone() == (1,)
