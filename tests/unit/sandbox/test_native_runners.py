from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.sandbox.runners import (
    DJANGO,
    PYTEST_REQUESTS_COMPAT,
    SYMPY,
    RunnerAdapter,
    RunnerConfig,
    RunnerOverrides,
    django_selector,
    parse_django,
    parse_sympy,
)
from codepilot.schemas.enums import ExecutionStatus, TestOutcome

DJANGO_OUT = """test_error (utils_tests.test_autoreload.TestMain) ... ERROR
test_fail (utils_tests.test_autoreload.TestMain) ... FAIL
test_ok (utils_tests.test_autoreload.TestMain) ... ok
test_skip (utils_tests.test_autoreload.TestMain) ... skipped 'not today'

======================================================================
ERROR: test_error (utils_tests.test_autoreload.TestMain)
----------------------------------------------------------------------
RuntimeError: boom

----------------------------------------------------------------------
Ran 4 tests in 0.004s

FAILED (failures=1, errors=1, skipped=1)
"""

SYMPY_OUT = """sympy/geometry/tests/test_point.py[4]
test_point /testbed/sympy/geometry/point.py:148: UserWarning: Dimension of (0, 0)
  warnings.warn(message)
ok
test_Point2D ok
test_issue_9214 F
test_issue_11617 not today s                                               [FAIL]

________________________________________________________________________________
____________ sympy/geometry/tests/test_point.py:test_issue_9214 _______________
AssertionError

 tests finished: 2 passed, 1 failed, 1 skipped, in 0.10 seconds
"""


def _workspace(tmp_path: Path) -> Path:
    file = tmp_path / "tests/utils_tests/test_autoreload.py"
    file.parent.mkdir(parents=True)
    file.write_text("", encoding="utf-8")
    return tmp_path


def test_django_statuses_and_canonical_paths(tmp_path: Path) -> None:
    result = parse_django(DJANGO_OUT, "", exit_code=1, workspace=_workspace(tmp_path))
    assert result.status is ExecutionStatus.FAIL
    outcomes = {test.node_id: test.outcome for test in result.tests}
    base = "tests/utils_tests/test_autoreload.py::TestMain"
    assert outcomes == {
        f"{base}::test_error": TestOutcome.ERROR,
        f"{base}::test_fail": TestOutcome.FAILED,
        f"{base}::test_ok": TestOutcome.PASSED,
        f"{base}::test_skip": TestOutcome.SKIPPED,
    }
    assert result.diagnostics["parse_counts"]["ran"] == 4


def test_django_requested_scope_must_be_present(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    wanted = ("tests/utils_tests/test_autoreload.py::TestMain::test_ok",)
    good = parse_django(DJANGO_OUT, "", exit_code=1, workspace=workspace, requested=wanted)
    assert [test.node_id for test in good.tests] == list(wanted)
    missing = parse_django(
        DJANGO_OUT,
        "",
        exit_code=1,
        workspace=workspace,
        requested=("tests/utils_tests/test_autoreload.py::TestMain::test_missing",),
    )
    assert missing.status is ExecutionStatus.COLLECTION_ERROR
    assert missing.error_reason == "REQUESTED_TESTS_NOT_COLLECTED"


def test_django_selector_round_trips_paths_and_native_labels(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert (
        django_selector("tests/utils_tests/test_autoreload.py::TestMain::test_ok", workspace)
        == "utils_tests.test_autoreload.TestMain.test_ok"
    )
    assert (
        django_selector("utils_tests.test_autoreload", workspace) == "utils_tests.test_autoreload"
    )


def test_django_zero_tests_is_a_collection_error(tmp_path: Path) -> None:
    result = parse_django(
        "Ran 0 tests in 0.001s\n\nOK\n", "", exit_code=0, workspace=_workspace(tmp_path)
    )
    assert result.status is ExecutionStatus.COLLECTION_ERROR
    assert result.error_reason == "NO_TESTS_COLLECTED"


def test_django_exit_zero_with_failures_is_a_contradiction(tmp_path: Path) -> None:
    result = parse_django(DJANGO_OUT, "", exit_code=0, workspace=_workspace(tmp_path))
    assert result.status is ExecutionStatus.FAIL
    assert result.error_reason == "EXIT_CODE_OUTCOME_CONTRADICTION"


def test_sympy_interleaved_warning_status_and_summary() -> None:
    result = parse_sympy(SYMPY_OUT, "", exit_code=1, workspace=Path())
    assert result.status is ExecutionStatus.FAIL
    outcomes = {test.node_id: test.outcome for test in result.tests}
    base = "sympy/geometry/tests/test_point.py"
    assert outcomes[f"{base}::test_point"] is TestOutcome.PASSED
    assert outcomes[f"{base}::test_Point2D"] is TestOutcome.PASSED
    assert outcomes[f"{base}::test_issue_9214"] is TestOutcome.FAILED
    assert outcomes[f"{base}::test_issue_11617"] is TestOutcome.SKIPPED
    assert result.diagnostics["parse_counts"]["summary"]["failed"] == 1


def test_sympy_zero_tests_is_a_collection_error() -> None:
    result = parse_sympy(
        "sympy/geometry/tests/test_point.py[0]\n\n"
        "================== tests finished: 0 passed, in 0.00 seconds ==================\n",
        "",
        exit_code=0,
        workspace=Path(),
    )
    assert result.status is ExecutionStatus.COLLECTION_ERROR
    assert result.error_reason == "NO_TESTS_COLLECTED"


def test_sympy_exit_zero_with_failure_is_a_contradiction() -> None:
    result = parse_sympy(SYMPY_OUT, "", exit_code=0, workspace=Path())
    assert result.status is ExecutionStatus.FAIL
    assert result.error_reason == "EXIT_CODE_OUTCOME_CONTRADICTION"


def test_sympy_timeout_keeps_partial_results() -> None:
    partial = SYMPY_OUT.split("test_issue_9214 F")[0]
    result = parse_sympy(partial, "", exit_code=None, workspace=Path(), timed_out=True)
    assert result.status is ExecutionStatus.TIMEOUT
    assert {test.outcome for test in result.tests} == {TestOutcome.PASSED}


def test_requests_adapter_only_inserts_the_scoped_plugin(tmp_path: Path) -> None:
    config = RunnerConfig(
        kind=PYTEST_REQUESTS_COMPAT,
        test_command="pytest -rA --junitxml=/out/junit.xml",
        preflight_scope=("tests/test_utils.py",),
    )
    adapter = RunnerAdapter(config)
    command = adapter.build_command(
        "/opt/miniconda3/envs/testbed/bin/python -m pytest -rA --junitxml=/out/junit.xml",
        (),
        workspace=tmp_path,
    )
    assert "-p codepilot_requests_compat" in command
    assert command.count("-p codepilot_requests_compat") == 1


def test_native_adapter_builds_runner_specific_commands(tmp_path: Path) -> None:
    django = RunnerAdapter(
        RunnerConfig(
            kind=DJANGO,
            test_command="./tests/runtests.py --verbosity 2 --settings=test_sqlite",
        )
    )
    command = django.build_command(
        "ignored",
        ("tests/utils_tests/test_autoreload.py::TestMain::test_ok",),
        workspace=_workspace(tmp_path),
    )
    assert command.endswith("utils_tests.test_autoreload.TestMain.test_ok")
    sympy = RunnerAdapter(RunnerConfig(kind=SYMPY, test_command="bin/test -C --verbose"))
    command = sympy.build_command(
        "ignored",
        ("sympy/geometry/tests/test_point.py::test_Point2D",),
        workspace=tmp_path,
    )
    assert command.endswith("sympy/geometry/tests/test_point.py")


def test_runner_overrides_require_known_kinds_and_exact_coverage(tmp_path: Path) -> None:
    path = tmp_path / "runners.json"
    path.write_text(
        '{"schema_version": 1, "instances": {"a__b-1": '
        '{"kind": "django", "test_command": "bin/test"}}}',
        encoding="utf-8",
    )
    overrides = RunnerOverrides.load(path)
    assert overrides is not None
    assert overrides.for_instance("a__b-1").kind == DJANGO
    assert overrides.sha256 == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    path.write_text(
        '{"schema_version": 1, "instances": {"a__b-1": '
        '{"kind": "unknown", "test_command": "bin/test"}}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        RunnerOverrides.load(path)


def test_generated_reproduction_scope_uses_pytest_contract_only():
    from codepilot.sandbox.runners import reproduction_only

    assert reproduction_only(("tests/_codepilot_repro/test_issue.py::test_x",))
    assert not reproduction_only(())
    assert not reproduction_only(("tests/utils_tests/test_autoreload.py::Test::test_x",))
    assert not reproduction_only(("tests/_codepilot_repro_extra/test_issue.py::test_x",))
    assert not reproduction_only(
        ("tests/_codepilot_repro/test_issue.py::test_x", "tests/test_existing.py")
    )
