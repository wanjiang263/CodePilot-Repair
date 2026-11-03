"""Phase 33 regression tests for Windows transient file-lock flakes.

Root causes these tests pin down (evidence: .codex-pytest-tmp/phase33/evidence/t06/):

1. ``PermissionError [WinError 5]`` when the demo cache staging directory is
   renamed to its final name: anti-virus scanners hold handles without
   ``FILE_SHARE_DELETE`` on the ``.git`` objects git just wrote. Phase 32/33
   saw this fail a whole worker run (``worker_exception`` -> run failed ->
   no ``patch_final.diff`` -> patch route answered 404 instead of 400).
   ``materialize_demo_repo`` must retry the rename with backoff.
2. The same transient locks make ``_force_rmtree`` fail on artifact cleanup,
   which the cleanup route reports as ``artifact_cleanup_failed`` and returns
   ``deleted == []``. It must retry with backoff and, when the lock persists,
   raise an ``OSError`` that names the surviving files.
3. ``logging.basicConfig(stream=sys.stderr)`` froze whatever stream object was
   current at configuration time. Under pytest that is a per-test capture
   buffer which is closed at teardown; background threads (e.g. the anyio
   portal serving TestClient requests) then died with
   ``ValueError: I/O operation on closed file``. The stdlib handler must write
   through the live ``_LiveStderr`` proxy.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from codepilot.api import demo as demo_mod
from codepilot.api import settings_routes
from codepilot.observability.logging import configure_logging


def test_materialize_demo_repo_retries_transient_rename_lock(tmp_path, monkeypatch):
    source = tmp_path / "mini_demo"
    (source / "src").mkdir(parents=True)
    (source / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    cache_root = tmp_path / "cache"

    real_replace = Path.replace
    failures = {"count": 0}

    def flaky_replace(self, target):
        is_staging = self.name.startswith("mini_demo-") and ".tmp-" in self.name
        if is_staging and failures["count"] < 2:
            failures["count"] += 1
            raise PermissionError(5, "Access is denied", str(self), 5, str(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    sleeps: list[float] = []
    monkeypatch.setattr(demo_mod.time, "sleep", sleeps.append)

    target_dir, head = demo_mod.materialize_demo_repo(source, cache_root)

    assert failures["count"] == 2
    assert sleeps == pytest.approx([0.1, 0.2])
    assert (target_dir / ".git").is_dir()
    assert len(head) == 40
    assert not list((cache_root / "demos").glob("*.tmp-*"))


def test_materialize_demo_repo_reuses_winner_when_target_appears(tmp_path, monkeypatch):
    source = tmp_path / "mini_demo"
    source.mkdir()
    (source / "README.md").write_text("demo\n", encoding="utf-8")
    cache_root = tmp_path / "cache"

    real_replace = Path.replace
    rename_calls = {"count": 0}

    def losing_replace(self, target):
        # First rename attempt fails and, while we back off, a concurrent
        # materialization completes the target directory.
        if self.name.startswith("mini_demo-") and ".tmp-" in self.name:
            rename_calls["count"] += 1
            if rename_calls["count"] == 1:
                final = Path(target)
                (final / ".git").mkdir(parents=True)
                raise PermissionError(5, "Access is denied", str(self), 5, str(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", losing_replace)
    monkeypatch.setattr(demo_mod.time, "sleep", lambda _s: None)

    target_dir, _head = demo_mod.materialize_demo_repo(source, cache_root)

    assert rename_calls["count"] == 1
    assert target_dir == (cache_root / "demos" / target_dir.name).resolve()
    assert not list((cache_root / "demos").glob("*.tmp-*"))


def test_force_rmtree_retries_transient_share_violation(tmp_path, monkeypatch):
    target = tmp_path / "runs" / "run_x"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "state.json").write_text("{}", encoding="utf-8")

    real_rmtree = settings_routes.shutil.rmtree
    calls = {"count": 0}

    def flaky_rmtree(path, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            return None  # simulate locked files: nothing gets removed
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(settings_routes.shutil, "rmtree", flaky_rmtree)
    sleeps: list[float] = []
    monkeypatch.setattr(settings_routes.time, "sleep", sleeps.append)

    settings_routes._force_rmtree(target)

    assert calls["count"] == 3
    assert sleeps == pytest.approx([0.1, 0.2])
    assert not target.exists()


def test_force_rmtree_reports_survivors_when_lock_persists(tmp_path, monkeypatch):
    target = tmp_path / "runs" / "run_y"
    target.mkdir(parents=True)
    (target / "locked.log").write_text("x", encoding="utf-8")

    monkeypatch.setattr(settings_routes.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(settings_routes.time, "sleep", lambda _s: None)

    with pytest.raises(OSError, match=r"locked\.log"):
        settings_routes._force_rmtree(target, attempts=3)


def test_stdlib_logging_handler_follows_live_stderr(monkeypatch):
    configure_logging(level="INFO")
    stream = io.StringIO()
    # Simulate pytest teardown/capture swap AFTER configuration: the handler
    # must follow the *current* sys.stderr instead of the frozen stream object.
    monkeypatch.setattr("sys.stderr", stream)
    logging.getLogger("codepilot.api").info("api.request")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "api.request" in stream.getvalue()
