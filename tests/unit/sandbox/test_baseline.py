from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.sandbox.baseline import build_test_baseline
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult


def execution(
    *cases: tuple[str, TestOutcome], status: ExecutionStatus = ExecutionStatus.FAIL
) -> ExecutionResult:
    tests = [TestCaseResult(node_id=node, outcome=outcome, duration=0) for node, outcome in cases]
    return ExecutionResult(status=status, tests=tests)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (TestOutcome.PASSED, TestOutcome.FAILED),
        (TestOutcome.FAILED, TestOutcome.PASSED),
        (TestOutcome.ERROR, TestOutcome.SKIPPED),
    ],
)
def test_two_run_changes_are_flaky(tmp_path: Path, first: TestOutcome, second: TestOutcome) -> None:
    sandbox = FakeSandbox([execution(("test_node", first)), execution(("test_node", second))])
    environment = sandbox.build_env(
        repository="repo", base_commit="base", workspace=tmp_path, env_spec=EnvSpec()
    )
    baseline = build_test_baseline(
        sandbox,
        base_commit="base",
        workspace=tmp_path,
        environment=environment,
        env_spec=EnvSpec(),
    )
    assert baseline.flaky == {"test_node"}
    assert baseline.status_differences["test_node"] == (first, second)
    assert len(baseline.run_outcomes) == 2
    assert len(sandbox.run_calls) == 2


def test_missing_test_in_one_run_is_flaky(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        [
            execution(("stable", TestOutcome.PASSED), ("vanished", TestOutcome.PASSED)),
            execution(("stable", TestOutcome.PASSED)),
        ]
    )
    environment = sandbox.build_env(
        repository="repo", base_commit="base", workspace=tmp_path, env_spec=EnvSpec()
    )
    baseline = build_test_baseline(
        sandbox,
        base_commit="base",
        workspace=tmp_path,
        environment=environment,
        env_spec=EnvSpec(),
    )
    assert baseline.flaky == {"vanished"}


def test_baseline_records_terminal_environment_failure(tmp_path: Path) -> None:
    sandbox = FakeSandbox.from_statuses([ExecutionStatus.ENV_ERROR, ExecutionStatus.PASS])
    environment = sandbox.build_env(
        repository="repo", base_commit="base", workspace=tmp_path, env_spec=EnvSpec()
    )
    baseline = build_test_baseline(
        sandbox,
        base_commit="base",
        workspace=tmp_path,
        environment=environment,
        env_spec=EnvSpec(),
    )
    assert baseline.complete is False
    assert "status=ENV_ERROR" in baseline.evidence[0]
    assert len(sandbox.run_calls) == 1
