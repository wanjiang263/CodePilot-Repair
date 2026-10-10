"""R-23 notes replace exactly one repository/commit result."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "index_real_repo.py"
spec = importlib.util.spec_from_file_location("index_real_repo", SCRIPT)
assert spec is not None and spec.loader is not None
index_real_repo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(index_real_repo)


def test_write_notes_replaces_same_repo_commit(tmp_path: Path, monkeypatch) -> None:
    notes = tmp_path / "phase1.md"
    notes.write_text("# Existing evidence\n\nKeep the crash report.\n", encoding="utf-8")
    monkeypatch.setattr(index_real_repo, "NOTES", notes)
    index_real_repo.write_notes("old result", repo="flask", commit="a")
    index_real_repo.write_notes("new result", repo="flask", commit="a")
    once = notes.read_bytes()
    index_real_repo.write_notes("new result", repo="flask", commit="a")
    assert notes.read_bytes() == once
    assert b"old result" not in once
    assert once.count(b"new result") == 1
    assert b"Keep the crash report." in once


def test_write_notes_distinguishes_repo_and_commit(tmp_path: Path, monkeypatch) -> None:
    notes = tmp_path / "nested" / "phase1.md"
    monkeypatch.setattr(index_real_repo, "NOTES", notes)
    index_real_repo.write_notes("flask-a", repo="flask", commit="a")
    index_real_repo.write_notes("requests-a", repo="requests", commit="a")
    index_real_repo.write_notes("flask-b", repo="flask", commit="b")
    index_real_repo.write_notes("updated-a", repo="flask", commit="a")
    content = notes.read_text(encoding="utf-8")
    assert "flask-a" not in content
    assert all(body in content for body in ("updated-a", "requests-a", "flask-b"))
