"""Phase 19: deterministic closure, ordering and attribution defects.

These tests are written but intentionally not executed in this static round.
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, TARGET, build_harness, edit, passed_result

from codepilot.agents.reviewer import PatchReviewer
from codepilot.graph import nodes
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.stop_rules import patch_hash
from codepilot.graph.target_tests import select_target_tests
from codepilot.llm.fake import FakeLLM
from codepilot.report import verification_level
from codepilot.schemas.enums import ExecutionStatus, RunStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestBaseline, TestCaseResult
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.review import ReviewResult
from codepilot.schemas.state import Attempt
from codepilot.schemas.validation import ValidationResult

PATCH = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value\n"
)
ROUND = [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]


def _state_with_baseline(harness, baseline: TestBaseline):
    attempt = Attempt(
        iteration=1,
        patch_diff=PATCH,
        patch_hash=patch_hash(PATCH),
        validation=ValidationResult(passed=True),
    )
    return harness.initial.model_copy(
        update={
            "current_patch": PATCH,
            "attempts": [attempt],
            "plan": RepairPlan(
                root_cause_hypothesis="normalize is off by one",
                evidence=["src/pkg/core.py"],
                changes=[],
                tests_to_run=[TARGET],
                risk="low",
                alternatives=[],
            ),
            "baseline": baseline,
            "status": RunStatus.RUNNING,
        }
    )


def test_file_target_expansion_keeps_pytest_collection_order(tmp_path: Path) -> None:
    """A sorted baseline dict must not reorder session-scoped fixtures."""
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[ROUND],
        baseline_results=[],
    )
    first = f"{TARGET}::test_after_move_dir"
    second = f"{TARGET}::test_move_dir"
    third = f"{TARGET}::test_before_move_dir"
    failing = f"{TARGET}::test_already_failing"
    baseline = TestBaseline(
        base_commit=harness.base_commit,
        # Storage order is alphabetical; pytest actually ran these in the
        # order of run_outcomes.
        outcomes={
            failing: TestOutcome.FAILED,
            second: TestOutcome.PASSED,
            third: TestOutcome.PASSED,
            first: TestOutcome.PASSED,
        },
        run_outcomes=[
            {
                second: TestOutcome.PASSED,
                third: TestOutcome.PASSED,
                first: TestOutcome.PASSED,
                failing: TestOutcome.FAILED,
            }
        ],
    )
    state = _state_with_baseline(harness, baseline)
    try:
        selection = select_target_tests(
            state,
            policy=harness.deps.validator.policy,
            index=harness.deps.index,
            repo_graph=harness.deps.repo_graph,
            baseline=baseline,
        )
    finally:
        harness.close()

    assert selection.tests == (second, third, first)
    assert selection.baseline_failures == (failing,)


def test_l3_completion_requires_l2_to_cover_the_authorized_baseline(
    tmp_path: Path,
) -> None:
    node = f"{TARGET}::test_normalize"
    fixed = f"{TARGET}::test_issue_reproducer"
    harness = build_harness(
        tmp_path,
        results=[
            ExecutionResult(
                status=ExecutionStatus.PASS,
                tests=[
                    TestCaseResult(node_id=node, outcome=TestOutcome.PASSED, duration=0),
                    TestCaseResult(node_id=fixed, outcome=TestOutcome.PASSED, duration=0),
                ],
            )
        ],
        coder_rounds=[ROUND],
        baseline_results=[],
    )
    baseline = TestBaseline(
        base_commit=harness.base_commit,
        outcomes={node: TestOutcome.PASSED, fixed: TestOutcome.FAILED},
        run_outcomes=[{node: TestOutcome.PASSED, fixed: TestOutcome.FAILED}],
        duration_seconds=10_000,
    )
    state = _state_with_baseline(harness, baseline).model_copy(
        update={
            # The harness workspace has no applied patch; operation identity
            # compares the live diff with current_patch.
            "current_patch": "",
            "execution": ExecutionResult(status=ExecutionStatus.PASS),
            "impact": ImpactAnalysis(related_tests=[TARGET]),
        }
    )
    try:
        result = RegressionRunner().run(state, harness.deps)
    finally:
        harness.close()

    # L2 covered the whole authorized scope, and L3 has no candidates. That is
    # a complete full regression, not an "empty L3 sample" failure.
    assert result.complete is True
    assert result.status == "completed"
    assert "L3_full" in {level.value for level in result.levels_run}
    assert result.unexecuted_tests == []
    # The baseline-failing test was excluded from L1 (file target only takes
    # PASS nodes) but is still recorded as independent fix evidence.
    assert result.fixed_tests == [fixed]


def test_l2_missing_one_authorized_node_is_not_complete(tmp_path: Path) -> None:
    first = f"{TARGET}::test_normalize"
    second = f"{TARGET}::test_other"
    harness = build_harness(
        tmp_path,
        results=[
            ExecutionResult(
                status=ExecutionStatus.PASS,
                tests=[TestCaseResult(node_id=first, outcome=TestOutcome.PASSED, duration=0)],
            )
        ],
        coder_rounds=[ROUND],
        baseline_results=[],
    )
    baseline = TestBaseline(
        base_commit=harness.base_commit,
        outcomes={first: TestOutcome.PASSED, second: TestOutcome.PASSED},
        run_outcomes=[{first: TestOutcome.PASSED, second: TestOutcome.PASSED}],
    )
    state = _state_with_baseline(harness, baseline).model_copy(
        update={
            "current_patch": "",
            "execution": ExecutionResult(status=ExecutionStatus.PASS),
            "impact": ImpactAnalysis(related_tests=[TARGET]),
        }
    )
    try:
        result = RegressionRunner().run(state, harness.deps)
    finally:
        harness.close()

    assert result.complete is False
    assert result.unexecuted_tests == [second]
    assert result.failure_reason is not None


def test_collection_attribution_uses_frames_not_substrings() -> None:
    execution = ExecutionResult(
        status=ExecutionStatus.COLLECTION_ERROR,
        stderr_tail=(
            'ERROR collecting tests/test_core.py\n'
            '  File "/testbed/tests/test_core.py", line 2\n'
            "ImportError: something in docs/path.py was mentioned\n"
        ),
    )
    # docs/path.py is only an incidental substring; the frame is a different
    # file, so this must stay infrastructure/unknown, not model-caused.
    result = nodes._target_execution_attribution(execution, ("src/pkg/path.py",))
    assert result is not None and result[1] == "workspace"

    execution.stderr_tail = (
        '  File "/testbed/src/pkg/path.py", line 10\n'
        "SyntaxError: invalid syntax"
    )
    result = nodes._target_execution_attribution(execution, ("src/pkg/path.py",))
    assert result == (
        "model_output_invalid",
        "model",
        "COLLECTION_ERROR caused by the patched code; entering diagnosis",
    )


def test_review_without_a_patch_hash_does_not_verify(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND],
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(
        FakeLLM([{"approved": True, "summary": "looks good"}])
    )
    state = harness.run()
    assert state.review is not None
    try:
        unbound = state.model_copy(
            update={
                "review": state.review.model_copy(update={"reviewed_patch_hash": ""}),
                "status": RunStatus.SUCCEEDED,
            }
        )
        assert verification_level(unbound) != "m2_verified"
        unapproved = state.model_copy(
            update={
                "review": ReviewResult(
                    approved=False,
                    summary="changes requested",
                    reviewed_patch_hash=patch_hash(state.current_patch),
                ),
                "status": RunStatus.SUCCEEDED,
            }
        )
        assert verification_level(unapproved) != "m2_verified"
    finally:
        harness.close()
