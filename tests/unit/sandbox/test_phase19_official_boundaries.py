"""Phase 19: official-sandbox status and archive-boundary regressions (not executed)."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

from codepilot.sandbox.official import (
    _extract_suffix,
    _reclassify_pytest_fixture_dependency,
)
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult


def _result(status: ExecutionStatus, *, exit_code: int, outcome: TestOutcome) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        exit_code=exit_code,
        error_reason="MISSING_THIRD_PARTY_DEPENDENCY"
        if status is ExecutionStatus.ENV_ERROR
        else None,
        tests=[TestCaseResult(node_id="tests/test_a.py::test_x", outcome=outcome, duration=0)],
    )


def test_all_skipped_exit_zero_is_pass_for_pytest_optional_imports() -> None:
    result = _result(ExecutionStatus.ENV_ERROR, exit_code=0, outcome=TestOutcome.SKIPPED)
    fixed = _reclassify_pytest_fixture_dependency("pytest-dev__pytest-11148", result)
    assert fixed.status is ExecutionStatus.PASS
    assert fixed.error_reason is None
    assert fixed.diagnostics["reclassified_from"] == "ENV_ERROR"


def test_failed_exit_is_still_fail() -> None:
    result = _result(ExecutionStatus.ENV_ERROR, exit_code=1, outcome=TestOutcome.FAILED)
    fixed = _reclassify_pytest_fixture_dependency("pytest-dev__pytest-11148", result)
    assert fixed.status is ExecutionStatus.FAIL


def _tar_bytes(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, payload in members:
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return buffer.getvalue()


def _file(name: str, payload: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


def test_archive_extraction_skips_traversal_and_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "root").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = workspace / "root" / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt" or exc.winerror != 1314:
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    link = tarfile.TarInfo("root/skip-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp/elsewhere"
    hard = tarfile.TarInfo("root/skip-hard")
    hard.type = tarfile.LNKTYPE
    hard.linkname = "root/ok.txt"

    archive = _tar_bytes(
        [
            _file("root/ok.txt", b"ok"),
            _file("root/../escape.txt", b"escape"),
            _file("root/linked/pwn.txt", b"pwn"),
            (link, None),
            (hard, None),
        ]
    )
    extracted = _extract_suffix(archive, workspace, prefix="root", suffix=".txt")

    assert extracted == 1
    assert (workspace / "root" / "ok.txt").read_text() == "ok"
    assert not (workspace / "escape.txt").exists()
    assert not (outside / "pwn.txt").exists()
