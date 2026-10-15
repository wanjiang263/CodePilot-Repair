"""DA-010 controlled sample; this is not a real-model diagnosis evaluation."""

from datetime import UTC, datetime

import pytest

from codepilot.graph.progress import ProgressTracker
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult


@pytest.mark.parametrize(
    "message,collision,repeated", [("assert 2 == 3", False, True), ("assert 7 == 9", True, False)]
)
def test_same_signature_and_node_different_raw_failure(message, collision, repeated):
    def attempt(number, msg):
        return Attempt(
            iteration=number,
            validation=ValidationResult(passed=True),
            failure_signatures=["same-normalized-signature"],
            execution=ExecutionResult(
                status=ExecutionStatus.FAIL,
                tests=[
                    TestCaseResult(
                        node_id="tests/test_other.py::test_other",
                        outcome=TestOutcome.FAILED,
                        duration=0,
                        failure_type="AssertionError",
                        message=msg,
                    )
                ],
            ),
        )

    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="fixture",
        base_commit="base",
        issue="test",
        started_at=datetime.now(UTC),
        attempts=[attempt(1, "assert 2 == 3"), attempt(2, message)],
    )
    result = ProgressTracker().evaluate(state)
    assert result.signature_collision_suspected is collision
    assert result.repeated_failure is repeated
