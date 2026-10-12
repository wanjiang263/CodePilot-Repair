from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.sandbox.junit import JUnitReport
from codepilot.sandbox.status import classify_execution, execution_route
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import TestCaseResult


def report(outcome: TestOutcome = TestOutcome.PASSED) -> JUnitReport:
    return JUnitReport(
        tests=[TestCaseResult(node_id="tests/test_x.py::test_x", outcome=outcome, duration=0)]
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"exit_code": 0, "junit": report()}, ExecutionStatus.PASS),
        ({"exit_code": 1, "junit": report(TestOutcome.FAILED)}, ExecutionStatus.FAIL),
        ({"exit_code": None, "junit": None, "timed_out": True}, ExecutionStatus.TIMEOUT),
        ({"exit_code": 2, "junit": report(TestOutcome.ERROR)}, ExecutionStatus.COLLECTION_ERROR),
        (
            {"exit_code": None, "junit": None, "environment_error": "install failed"},
            ExecutionStatus.ENV_ERROR,
        ),
        (
            {"exit_code": None, "junit": None, "sandbox_error": "daemon unavailable"},
            ExecutionStatus.SANDBOX_ERROR,
        ),
    ],
)
def test_all_six_execution_states(kwargs: dict[str, object], expected: ExecutionStatus) -> None:
    assert classify_execution(**kwargs).status is expected  # type: ignore[arg-type]


def test_exit_zero_cannot_override_failing_junit() -> None:
    decision = classify_execution(exit_code=0, junit=report(TestOutcome.FAILED))
    assert decision.status is ExecutionStatus.FAIL
    assert decision.error_reason == "EXIT_CODE_JUNIT_CONTRADICTION"


def test_oom_uses_existing_status_with_independent_reason() -> None:
    decision = classify_execution(exit_code=137, junit=None, oom_killed=True)
    assert decision.status is ExecutionStatus.SANDBOX_ERROR
    assert decision.error_reason == "OOM_KILLED"
    assert decision.diagnostics["oom_killed"] is True


def test_timeout_and_oom_reasons_survive_cleanup_errors() -> None:
    timeout = classify_execution(
        exit_code=None,
        junit=None,
        timed_out=True,
        sandbox_error="force remove failed",
    )
    oom = classify_execution(
        exit_code=137,
        junit=None,
        oom_killed=True,
        sandbox_error="force remove failed",
    )
    assert timeout.status is ExecutionStatus.TIMEOUT
    assert timeout.error_reason == "EXECUTION_TIMEOUT"
    assert timeout.diagnostics["post_timeout_error"] == "force remove failed"
    assert oom.error_reason == "OOM_KILLED"
    assert oom.diagnostics["cleanup_error"] == "force remove failed"


def test_only_third_party_missing_dependency_is_env_error(tmp_path: Path) -> None:
    (tmp_path / "src" / "ourpkg").mkdir(parents=True)
    internal = classify_execution(
        exit_code=1,
        junit=report(TestOutcome.ERROR),
        workspace=tmp_path,
        diagnostic_text="ModuleNotFoundError: No module named 'ourpkg'",
    )
    external = classify_execution(
        exit_code=1,
        junit=report(TestOutcome.ERROR),
        workspace=tmp_path,
        diagnostic_text="ModuleNotFoundError: No module named 'missing_vendor'",
    )
    assert internal.status is ExecutionStatus.FAIL
    assert external.status is ExecutionStatus.ENV_ERROR


@pytest.mark.parametrize(
    ("status", "route"),
    [
        (ExecutionStatus.PASS, "regression"),
        (ExecutionStatus.FAIL, "diagnose"),
        (ExecutionStatus.TIMEOUT, "diagnose"),
        (ExecutionStatus.COLLECTION_ERROR, "diagnose"),
        (ExecutionStatus.ENV_ERROR, "finalize"),
        (ExecutionStatus.SANDBOX_ERROR, "finalize"),
    ],
)
def test_environment_error_routing_is_immediate(status: ExecutionStatus, route: str) -> None:
    assert execution_route(status) == route
