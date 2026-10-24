"""A status label alone cannot manufacture an M2 verification claim."""

from datetime import UTC, datetime

import pytest

from codepilot.graph.stop_rules import patch_hash
from codepilot.report import RunReport
from codepilot.schemas.enums import ExecutionStatus, RunStatus
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.regression import RegressionMode, RegressionResult
from codepilot.schemas.review import ReviewIssue, ReviewResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult


@pytest.mark.parametrize(
    "missing", ["target", "validation", "regression", "review", "high", "new_failure"]
)
def test_succeeded_without_required_evidence_is_not_m2(missing):
    state = RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="base",
        issue="issue",
        started_at=datetime.now(UTC),
        status=RunStatus.SUCCEEDED,
        execution=ExecutionResult(status=ExecutionStatus.PASS),
        attempts=[Attempt(iteration=1, validation=ValidationResult(passed=True))],
        regression=RegressionResult(complete=True),
        review=ReviewResult(approved=True),
    )
    if missing == "target":
        state.execution = None
    elif missing == "validation":
        state.attempts[0].validation.passed = False
    elif missing == "regression":
        state.regression.complete = False
    elif missing == "review":
        state.review = None
    elif missing == "high":
        state.review.issues = [
            ReviewIssue(severity="high", file="core.py", line=1, description="Unresolved")
        ]
    else:
        state.regression.new_failures = ["tests/test_core.py::test_core"]
    report = RunReport.from_state(
        state, repository="repo", git_diff="", modified_files=[], simulated_llm=True
    )
    assert report.verification_level not in {"m2_verified", "sampled_verified"}


@pytest.mark.parametrize(
    "mode,level",
    [(RegressionMode.FULL, "m2_verified"), (RegressionMode.SAMPLED, "sampled_verified")],
)
def test_complete_evidence_preserves_verification_scope(mode, level):
    state = RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="base",
        issue="issue",
        started_at=datetime.now(UTC),
        status=RunStatus.SUCCEEDED,
        current_patch="patch",
        execution=ExecutionResult(status=ExecutionStatus.PASS),
        attempts=[Attempt(iteration=1, validation=ValidationResult(passed=True))],
        regression=RegressionResult(complete=True, mode=mode),
        # The report's git_diff is "patch"; a complete-evidence fixture must
        # bind the review to that identity, not rely on an empty legacy hash.
        review=ReviewResult(approved=True, reviewed_patch_hash=patch_hash("patch")),
    )
    report = RunReport.from_state(
        state, repository="repo", git_diff="patch", modified_files=[], simulated_llm=True
    )
    assert report.verification_level == level
    assert report.patch_hash
    assert report.simulated_llm
