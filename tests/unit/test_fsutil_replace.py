from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot import fsutil


def _winerror(code: int) -> OSError:
    error = OSError(errno.EACCES, os.strerror(errno.EACCES))
    error.winerror = code  # type: ignore[attr-defined]
    return error


def test_transient_handle_error_is_retried_with_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    calls = {"count": 0}
    real_replace = Path.replace

    def flaky(self: Path, other: Path) -> Path:
        calls["count"] += 1
        if calls["count"] < 3:
            raise _winerror(32)
        return real_replace(self, other)

    monkeypatch.setattr(fsutil, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Path, "replace", flaky)
    fsutil.replace_path(source, target, attempts=5, base_delay=0.0)
    assert calls["count"] == 3
    assert target.is_dir()


def test_persistent_handle_error_raises_after_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    calls = {"count": 0}

    def always(self: Path, other: Path) -> Path:
        calls["count"] += 1
        raise _winerror(5)

    monkeypatch.setattr(fsutil, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Path, "replace", always)
    with pytest.raises(OSError):
        fsutil.replace_path(source, tmp_path / "target", attempts=3, base_delay=0.0)
    assert calls["count"] == 3


def test_non_transient_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    def missing(self: Path, other: Path) -> Path:
        calls["count"] += 1
        raise _winerror(2)

    monkeypatch.setattr(fsutil, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Path, "replace", missing)
    with pytest.raises(OSError):
        fsutil.replace_path(tmp_path / "a", tmp_path / "b", attempts=5, base_delay=0.0)
    assert calls["count"] == 1
