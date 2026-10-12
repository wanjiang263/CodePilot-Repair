from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.artifacts import STDERR_LOG, STDOUT_LOG, ArtifactStore
from codepilot.sandbox.artifacts import persist_logs, tail_utf8


def test_tail_is_utf8_safe_and_keeps_the_end() -> None:
    result = tail_utf8("头🙂middle尾", 8)
    assert result.endswith("尾")
    assert len(result.encode("utf-8", errors="replace")) <= 10  # replacement may span boundary


def test_full_logs_are_redacted_and_tail_is_bounded(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, "run")
    logs = persist_logs(
        store,
        iteration=1,
        stdout="prefix sk-secret123456 suffix " + "x" * 50,
        stderr="password=verysecretvalue\udcff",
        tail_bytes=12,
    )
    stdout = store.read_text(store.attempt_relpath(1, STDOUT_LOG))
    stderr = store.read_text(store.attempt_relpath(1, STDERR_LOG))
    assert "secret123456" not in stdout
    assert "verysecretvalue" not in stderr
    assert logs.stdout_tail == "x" * 12
    assert Path(logs.artifact_dir).is_dir()


def test_log_write_failure_is_not_silently_turned_into_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path, "run")

    def fail(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_text", fail)
    with pytest.raises(OSError, match="disk full"):
        persist_logs(store, iteration=1, stdout="x", stderr="y", tail_bytes=1)
