"""Image-built artifacts must be restorable without touching source files."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from codepilot.sandbox.official import _extract_single_file, _extract_suffix


def _tar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_extract_suffix_keeps_only_matching_files(tmp_path: Path) -> None:
    archive = _tar(
        {
            "sklearn/a.so": b"a",
            "sklearn/nested/b.so": b"b",
            "sklearn/a.py": b"source",
            "other/c.so": b"c",
        }
    )

    count = _extract_suffix(archive, tmp_path, prefix="sklearn", suffix=".so")

    assert count == 2
    assert (tmp_path / "sklearn/a.so").read_bytes() == b"a"
    assert (tmp_path / "sklearn/nested/b.so").read_bytes() == b"b"
    assert not (tmp_path / "sklearn/a.py").exists()
    assert not (tmp_path / "other/c.so").exists()


def test_extract_suffix_never_overwrites_existing_files(tmp_path: Path) -> None:
    target = tmp_path / "sklearn/a.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep")
    archive = _tar({"sklearn/a.so": b"new", "sklearn/b.so": b"b"})

    count = _extract_suffix(archive, tmp_path, prefix="sklearn", suffix=".so")

    assert count == 1
    assert target.read_bytes() == b"keep"
    assert (tmp_path / "sklearn/b.so").read_bytes() == b"b"


def test_extract_suffix_refuses_parent_escape(tmp_path: Path) -> None:
    archive = _tar({"sklearn/../../escape.so": b"x", "sklearn/ok.so": b"y"})

    count = _extract_suffix(archive, tmp_path, prefix="sklearn", suffix=".so")

    assert count == 1
    assert (tmp_path / "sklearn/ok.so").read_bytes() == b"y"
    assert not (tmp_path.parent / "escape.so").exists()


def test_extract_single_file_writes_version_file(tmp_path: Path) -> None:
    archive = _tar({"_version.py": b"version = '1.0'\n"})
    target = tmp_path / "src/_pytest/_version.py"

    assert _extract_single_file(archive, target) == 1
    assert target.read_text(encoding="utf-8") == "version = '1.0'\n"
    assert _extract_single_file(archive, target) == 0


def test_copy_testdeps_is_optional_and_does_not_overwrite(tmp_path: Path) -> None:
    from codepilot.sandbox.official import _copy_testdeps

    assert _copy_testdeps(None, tmp_path) == 0
    assert _copy_testdeps(tmp_path / "missing", tmp_path) == 0
    source = tmp_path.parent / f"{tmp_path.name}-deps"
    source.mkdir()
    (source / "mod.py").write_text("value = 1\n", encoding="utf-8")
    target = tmp_path / "workspace"
    (target / ".git/info").mkdir(parents=True)
    (target / ".git/info/exclude").write_text("", encoding="utf-8")
    assert _copy_testdeps(source, target) == 1
    assert (target / "testdeps/mod.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _copy_testdeps(source, target) == 0


def test_copy_testdeps_is_excluded_from_git_status(tmp_path: Path) -> None:
    from codepilot.sandbox.official import _copy_testdeps

    source = tmp_path / "deps"
    source.mkdir()
    (source / "mod.py").write_text("value = 1\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / ".git/info").mkdir(parents=True)
    exclude = workspace / ".git/info/exclude"
    exclude.write_text("*.pyc\n", encoding="utf-8")

    assert _copy_testdeps(source, workspace) == 1
    assert "testdeps/" in exclude.read_text(encoding="utf-8").splitlines()


def test_pytest_fixture_missing_module_is_a_test_failure_not_environment() -> None:
    from codepilot.sandbox.official import _reclassify_pytest_fixture_dependency
    from codepilot.schemas.enums import ExecutionStatus, TestOutcome
    from codepilot.schemas.execution import ExecutionResult, TestCaseResult

    test = TestCaseResult(
        node_id="testing/test_import.py::test_x", outcome=TestOutcome.ERROR, duration=0.0
    )
    with_tests = ExecutionResult(
        status=ExecutionStatus.ENV_ERROR,
        error_reason="MISSING_THIRD_PARTY_DEPENDENCY",
        tests=[test],
    )
    without_tests = ExecutionResult(
        status=ExecutionStatus.ENV_ERROR,
        error_reason="MISSING_THIRD_PARTY_DEPENDENCY",
    )

    reclassified = _reclassify_pytest_fixture_dependency("pytest-dev__pytest-11148", with_tests)
    assert reclassified.status is ExecutionStatus.FAIL
    assert reclassified.error_reason is None
    assert reclassified.tests == [test]
    assert (
        _reclassify_pytest_fixture_dependency("pytest-dev__pytest-11148", without_tests).status
        is ExecutionStatus.ENV_ERROR
    )
    assert (
        _reclassify_pytest_fixture_dependency("scikit-learn__scikit-learn-12471", with_tests).status
        is ExecutionStatus.ENV_ERROR
    )
