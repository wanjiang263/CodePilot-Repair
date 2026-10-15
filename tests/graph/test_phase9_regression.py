from pathlib import Path

import pytest
from harness import FINISH, build_harness, edit, passed_result

from codepilot.agents.reviewer import PatchReviewer
from codepilot.graph.regression import RegressionRunner, new_failures
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.enums import ExecutionStatus, RunStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestBaseline, TestCaseResult
from codepilot.schemas.regression import RegressionResult


@pytest.mark.parametrize("before", list(TestOutcome))
@pytest.mark.parametrize("after", list(TestOutcome))
def test_exact_baseline_rule(before, after):
    baseline = TestBaseline(base_commit="base", outcomes={"tests/test_a.py::test_x[1]": before})
    execution = ExecutionResult(
        status=ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(node_id="tests/test_a.py::test_x[1]", outcome=after, duration=0),
            TestCaseResult(
                node_id="tests/test_a.py::test_x[2]", outcome=TestOutcome.FAILED, duration=0
            ),
        ],
    )
    expected = (
        ["tests/test_a.py::test_x[1]"]
        if before == TestOutcome.PASSED and after in {TestOutcome.FAILED, TestOutcome.ERROR}
        else []
    )
    assert new_failures(baseline, execution) == expected
    baseline.flaky.add("tests/test_a.py::test_x[1]")
    assert new_failures(baseline, execution) == []


def test_unrun_is_not_complete():
    result = RegressionResult()
    assert not result.complete
    assert result.status == "NOT RUN"
    assert RegressionResult.model_validate_json(result.model_dump_json()) == result


def test_full_graph(tmp_path: Path):
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(FakeLLM([{"approved": True, "summary": "checked"}]))
    state = harness.run()
    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.regression.complete
    assert state.review.approved
    assert state.test_runs == 5
    assert state.attempts[-1].regression == state.regression


@pytest.mark.parametrize(
    "outcomes,flaky",
    [
        (["failed", "passed"], True),
        (["passed", "failed"], True),
        (["failed", "failed"], False),
        (["error", "failed"], False),
    ],
)
def test_retry_classification_and_accounting(tmp_path, outcomes, flaky):
    from harness import assertion_failure

    def outcome(value):
        result = assertion_failure()
        result.tests[0].outcome = TestOutcome(value)
        if value == "passed":
            return passed_result()
        return result

    harness = build_harness(
        tmp_path,
        results=[passed_result(), assertion_failure(), *map(outcome, outcomes), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    result = RegressionRunner().run(state, harness.deps)
    assert ("tests/test_core.py" in result.flaky) is flaky
    assert bool(result.new_failures) is not flaky
    assert len(result.retries["tests/test_core.py"]) == 2
    assert harness.deps.test_runs == (7 if flaky else 6)


def test_budget_prevents_retry(tmp_path):
    from harness import assertion_failure

    from codepilot.schemas.budget import RepairBudget

    harness = build_harness(
        tmp_path,
        results=[passed_result(), assertion_failure()],
        budget=RepairBudget(max_test_runs=4),
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    result = RegressionRunner().run(state, harness.deps)
    assert not result.complete
    assert not result.flaky
    assert harness.deps.test_runs == 4


def test_operation_reuse_and_corruption(tmp_path):
    import sqlite3

    from codepilot.graph.operations import OperationUncertainError

    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    runner = RegressionRunner()
    first = runner.run(state, harness.deps)
    harness.deps.accounting.seed(state.test_runs)
    second = runner.run(state, harness.deps)
    assert first == second
    assert harness.deps.test_runs == 5
    with sqlite3.connect(harness.deps.artifacts.run_dir / "operations.sqlite") as db:
        db.execute("UPDATE operations SET hash='corrupted'")
    with pytest.raises(OperationUncertainError):
        runner.run(state, harness.deps)


@pytest.mark.parametrize("severity,expected", [("low", "succeeded"), ("medium", "succeeded")])
def test_non_high_review_does_not_repair(tmp_path, severity, expected):
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(
        FakeLLM(
            [
                {
                    "approved": False,
                    "issues": [
                        {
                            "severity": severity,
                            "file": "src/pkg/core.py",
                            "line": 2,
                            "description": "Consider documenting the identity contract",
                        }
                    ],
                }
            ]
        )
    )
    state = harness.run()
    assert state.status.value == expected, state.failure_reasons
    assert len(state.attempts) == 1


def test_invalid_review_evidence_fails_closed(tmp_path):
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(
        FakeLLM(
            [
                {
                    "approved": False,
                    "issues": [
                        {
                            "severity": "high",
                            "file": "made_up.py",
                            "line": 2,
                            "description": "fabricated",
                        }
                    ],
                }
            ]
        )
    )
    state = harness.run()
    assert state.status is not RunStatus.SUCCEEDED
    assert state.review is None
    assert state.review_repairs == 0


@pytest.mark.parametrize("second_high", [True, False])
def test_high_review_has_one_extra_repair(tmp_path, second_high):
    high = {
        "approved": False,
        "issues": [
            {
                "severity": "high",
                "file": "src/pkg/core.py",
                "line": 2,
                "description": "Review fixture requests explicit int normalization",
            }
        ],
    }
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[
            [edit("src/pkg/core.py", "return value - 1", "return value"), FINISH],
            [edit("src/pkg/core.py", "return value", "return int(value)"), FINISH],
        ],
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(
        FakeLLM([high, high if second_high else {"approved": True}])
    )
    state = harness.run()
    assert state.review_repairs == 1, state.failure_reasons
    assert len(state.attempts) == 2
    assert state.status is (RunStatus.STOPPED if second_high else RunStatus.SUCCEEDED), (
        state.failure_reasons
    )
    assert all(
        a.validation.passed and a.execution.status is ExecutionStatus.PASS for a in state.attempts
    )


def test_sampled_scope_is_disclosed(tmp_path):
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    state.baseline.duration_seconds = 100000
    state.impact.related_tests = []
    result = RegressionRunner(seed=17).run(state, harness.deps)
    assert result.complete
    assert result.mode.value == "sampled"
    assert result.seed == 17
    assert result.sampled_ratio == 1  # ceil(20% of one remaining test)
    assert result.executed_tests == ["tests/test_core.py"]


def test_incomplete_regression_does_not_call_reviewer(tmp_path):
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    with pytest.raises(ValueError, match="review requires"):
        PatchReviewer(FakeLLM([])).review(state, harness.deps)


@pytest.mark.parametrize("status", [ExecutionStatus.ENV_ERROR, ExecutionStatus.SANDBOX_ERROR])
def test_retry_environment_failure_never_becomes_flaky(tmp_path, status):
    from harness import assertion_failure

    harness = build_harness(
        tmp_path,
        results=[passed_result(), assertion_failure(), ExecutionResult(status=status)],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    result = RegressionRunner().run(state, harness.deps)
    assert not result.complete
    assert not result.flaky
    assert len(result.retries["tests/test_core.py"]) == 1
    assert harness.deps.test_runs == 5


def test_third_full_regression_is_not_executed(tmp_path):
    from codepilot.schemas.regression import RegressionLevel

    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    prior = state.attempts[0].model_copy(
        update={"regression": RegressionResult(levels_run=[RegressionLevel.FULL])}
    )
    state.attempts = [prior, prior.model_copy(), state.attempts[0]]
    state.impact.related_tests = []
    result = RegressionRunner().run(state, harness.deps)
    assert not result.complete
    assert result.failure_reason == "L3 full-run limit reached"
    assert harness.deps.test_runs == 3


def test_missing_baseline_is_not_a_clean_regression(tmp_path):
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    state.baseline = None
    result = RegressionRunner().run(state, harness.deps)
    assert not result.complete
    assert result.status == "NOT RUN"
    assert harness.deps.test_runs == 3
