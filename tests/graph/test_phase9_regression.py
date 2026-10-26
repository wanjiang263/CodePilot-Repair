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
    # L2 covered the whole authorized baseline (one test file), so its
    # execution is the L3 evidence; no duplicate full run is spent.
    assert state.test_runs == 4
    assert state.attempts[-1].regression == state.regression


def test_phase22_large_source_does_not_drop_review_patch(tmp_path: Path, monkeypatch):
    import harness as harness_module

    from codepilot.repo.gitcmd import run_git

    original_base_repo = harness_module._base_repo

    def lf_base(root):
        repo, _base = original_base_repo(root)
        (repo / ".gitattributes").write_bytes(b"*.py text eol=lf\n")
        run_git(["add", ".gitattributes"], cwd=repo)
        run_git(["add", "--renormalize", "."], cwd=repo)
        run_git(["commit", "-qm", "canonical fixture line endings"], cwd=repo)
        return repo, run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    monkeypatch.setattr(harness_module, "_base_repo", lf_base)

    monkeypatch.setattr(
        harness_module,
        "DEFECTIVE",
        harness_module.DEFECTIVE + ("# unrelated source context " + "x" * 80 + "\n") * 2000,
    )
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    reviewer = FakeLLM([{"approved": True, "summary": "checked"}])
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(reviewer)
    state = harness.run()
    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.current_patch in reviewer.calls[0].messages[0].content


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
    # baseline 2 + target 1 + L2 1 + two re-runs = 6; L3 reuses L2's
    # whole-baseline evidence instead of spending a seventh run.
    assert harness.deps.test_runs == 6


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


def test_retry_and_l3_fit_exactly_at_the_closure_boundary(tmp_path):
    from harness import assertion_failure

    from codepilot.schemas.budget import RepairBudget

    def outcome(value):
        result = assertion_failure()
        result.tests[0].outcome = TestOutcome(value)
        return passed_result() if value == "passed" else result

    harness = build_harness(
        tmp_path,
        # build_harness prepends the two baseline runs; these are target, L2
        # and the two re-runs.
        results=[
            passed_result(),
            assertion_failure(),
            *map(outcome, ["failed", "passed"]),
        ],
        budget=RepairBudget(max_test_runs=6),
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    result = RegressionRunner().run(state, harness.deps)
    assert result.flaky == {"tests/test_core.py"}
    assert result.new_failures == []
    assert result.complete
    # Six runs: baseline 2 + target + L2 + two re-runs. L2 covered the whole
    # authorized baseline, so L3 is recorded from that evidence for free.
    assert harness.deps.test_runs == 6


def test_retry_is_declined_when_it_would_eat_l3(tmp_path):
    from harness import assertion_failure

    from codepilot.schemas.budget import RepairBudget

    harness = build_harness(
        tmp_path,
        results=[passed_result(), assertion_failure()],
        budget=RepairBudget(max_test_runs=5),
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    result = RegressionRunner().run(state, harness.deps)
    assert not result.complete
    assert not result.flaky
    assert result.retry_notes and "flaky classification declined" in result.retry_notes[0]
    # baseline 2 + target + L2 = 4; one run remains, which cannot both classify
    # flaky (two re-runs) and leave room for the deterministic closure. No
    # retry is attempted and the run is reported incomplete, not "flaky".
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
    assert harness.deps.test_runs == 4
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


def test_review_issue_on_existing_unmodified_file_is_allowed(tmp_path):
    """未改动但真实存在的仓库文件可以作为 issue 证据；line=None 也合法。

    真实 GLM-5.3-Flash 的 reviewer 对测试文件提了一条建议性 issue
    （``tests/test_ledger.py``、line=null），旧校验只认 diff 文件，直接把整次
    运行判成 ``review evidence references an unverified file``。
    """
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
                    "approved": True,
                    "issues": [
                        {
                            "severity": "low",
                            "file": "tests/test_core.py",
                            "line": 2,
                            "description": "consider adding a regression assertion here",
                        },
                        {
                            "severity": "low",
                            "file": "src/pkg/helper.py",
                            "line": None,
                            "description": "file-level note",
                        },
                    ],
                }
            ]
        )
    )
    state = harness.run()
    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.review is not None
    assert len(state.review.issues) == 2


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


@pytest.mark.parametrize("missing", [False, True])
def test_phase23_full_file_dispatch_keeps_all_parameterized_nodes(tmp_path, missing):
    harness = build_harness(tmp_path, results=[], coder_rounds=[], baseline_results=[])
    first = r"tests/test_core.py::test_normalize[\\sphinxhref]"
    second = "tests/test_core.py::test_normalize[quote <&> value]"
    cases = [
        TestCaseResult(node_id=node, outcome=TestOutcome.PASSED, duration=0.1)
        for node in [first, second]
    ]
    after = ExecutionResult(
        status=ExecutionStatus.PASS, exit_code=0, tests=cases[:1] if missing else cases
    )
    from codepilot.sandbox.fake import FakeSandbox

    harness.deps.accounting.inner = FakeSandbox(results=[after])
    harness.deps.full_regression_file_scope = True
    baseline = TestBaseline(
        base_commit=harness.base_commit,
        outcomes={c.node_id: c.outcome for c in cases},
        run_outcomes=[{c.node_id: c.outcome for c in cases}],
    )
    state = harness.initial.model_copy(
        update={
            "baseline": baseline,
            "execution": ExecutionResult(status=ExecutionStatus.PASS, tests=[cases[0]]),
        }
    )
    try:
        result = RegressionRunner().run(state, harness.deps)
        assert result.complete is (not missing)
        assert harness.deps.accounting.inner.run_calls[0][1] == ("tests/test_core.py",)
        assert result.executions["L3"].diagnostics["authorized_node_count"] == 2
        if missing:
            assert second in result.unexecuted_tests
        else:
            assert set(result.executed_tests) == {first, second}
    finally:
        harness.close()
