"""G-06 / G-07：进度判定、振荡检测与签名误聚合控制（DA-010）。

进度判定的失效方式有两种，都得防：判太松，系统在两个错误之间来回振荡直到烧完
预算；判太严，正在推进的修复被 ``no_progress`` 提前掐掉。

签名碰撞那组用例是**负控制**：构造两个真实不同的故障，让冻结的 SHA1 把它们归成
同一个签名，然后断言系统不会仅凭签名相等就宣布"同一根因、停止修复"。不改 Phase 7
的算法（改了会让历史 run 的签名全部失效），只在停止这一侧加核验。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codepilot.failure.schemas import ParsedFailure
from codepilot.failure.signature import failure_signature
from codepilot.graph.progress import (
    ProgressTracker,
    build_progress_snapshot,
    round_evidence,
)
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.enums import ExecutionStatus, FailureCategory, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.progress import ProgressSnapshot, made_progress
from codepilot.schemas.regression import RegressionResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult


def _state(*snapshots: ProgressSnapshot | None, **updates) -> RepairState:
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    for index, snapshot in enumerate(snapshots, start=1):
        signatures = sorted(snapshot.signatures) if snapshot is not None else []
        state.attempts.append(
            Attempt(
                iteration=index,
                patch_hash=f"h{index}",
                validation=ValidationResult(passed=True),
                execution=_execution_for(signatures),
                failure_signatures=signatures,
                progress=snapshot,
            )
        )
    return state.model_copy(update=updates) if updates else state


def _execution_for(signatures: list[str]) -> ExecutionResult:
    cases = [
        TestCaseResult(
            node_id=f"tests/test_core.py::test_slot_{index}",
            outcome=TestOutcome.FAILED,
            duration=0.1,
            failure_type="AssertionError",
            message=f"slot {index} failed",
        )
        for index in range(len(signatures))
    ]
    return ExecutionResult(
        status=ExecutionStatus.FAIL if cases else ExecutionStatus.PASS,
        tests=cases,
        tests_failed=len(cases),
        artifact_dir=f"attempts/{'01' if cases else '00'}",
    )


def _snapshot(failed: int, *signatures: str, regressions: int = 0, measured: bool = True):
    return ProgressSnapshot(
        target_failed=failed,
        regression_new_failures=regressions,
        signatures=frozenset(signatures),
        regression_measured=measured,
    )


# ------------------------------------------------------------------ 三种进展


def test_fewer_target_failures_is_progress() -> None:
    assert made_progress(_snapshot(3, "A"), _snapshot(2, "B")) is True


def test_changed_signature_at_same_count_is_progress() -> None:
    """ImportError 修成 AssertionError 说明修复在推进。"""
    assert made_progress(_snapshot(1, "ImportError"), _snapshot(1, "AssertionError")) is True


def test_fewer_measured_regressions_is_progress() -> None:
    assert made_progress(_snapshot(1, "A", regressions=2), _snapshot(1, "A", regressions=1)) is True


def test_unmeasured_regression_is_not_compared_as_zero_new_failures() -> None:
    """Phase 9 未落地时，"未测量"不能被当成"回归变好了"或"回归变差了"。"""
    measured = _snapshot(1, "A", regressions=2, measured=True)
    unmeasured = _snapshot(1, "A", regressions=0, measured=False)
    assert made_progress(measured, unmeasured) is False
    assert made_progress(unmeasured, measured) is False


def test_identical_snapshot_is_no_progress() -> None:
    assert made_progress(_snapshot(2, "A"), _snapshot(2, "A")) is False


# ------------------------------------------------------------------ 快照构造


def test_snapshot_marks_regression_unmeasured_when_absent() -> None:
    execution = ExecutionResult(
        status=ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id="tests/test_core.py::test_a",
                outcome=TestOutcome.FAILED,
                duration=0.1,
            )
        ],
        tests_failed=1,
    )
    snapshot = build_progress_snapshot(execution, signatures=["sig-a"], regression=None)
    assert snapshot.target_failed == 1
    assert snapshot.regression_measured is False
    assert snapshot.regression_new_failures == 0


def test_snapshot_uses_real_regression_counts_when_measured() -> None:
    execution = ExecutionResult(status=ExecutionStatus.PASS)
    snapshot = build_progress_snapshot(
        execution,
        signatures=[],
        regression=RegressionResult(new_failures=["a::test_x", "b::test_y"]),
    )
    assert snapshot.regression_measured is True
    assert snapshot.regression_new_failures == 2


def test_missing_execution_is_not_counted_as_progress() -> None:
    """没有执行结果时 target_failed 记 0 但必须显式带未测量的回归，
    调用方不能把"没测到"读成"全绿"。"""
    snapshot = build_progress_snapshot(None, signatures=[], regression=None)
    assert snapshot.regression_measured is False
    assert snapshot.signatures == frozenset()


def test_environment_errors_do_not_produce_a_progress_claim() -> None:
    env = ExecutionResult(status=ExecutionStatus.ENV_ERROR, error_reason="boom")
    snapshot = build_progress_snapshot(env, signatures=["x"], regression=None)
    # 环境问题的失败数不能当作代码进度：它反映的是容器，不是补丁。
    assert snapshot.target_failed == 0
    assert snapshot.regression_measured is False


# ------------------------------------------------------------------ 振荡检测


@pytest.mark.parametrize(
    ("sets", "expected_round"),
    [
        ([{"A"}, {"B"}, {"A"}], 1),
        ([{"A"}, {"B"}, {"C"}, {"A"}], 1),
        ([{"A"}, {"B"}, {"C"}, {"B"}], 2),
        ([{"A"}, {"B"}, {"C"}, {"D"}, {"C"}], 3),
    ],
    ids=["short-period", "long-period-to-first", "long-period-to-middle", "four-rounds"],
)
def test_oscillation_over_any_earlier_round(sets: list[set[str]], expected_round: int) -> None:
    """A→B→A 与更长的 A→B→C→A 都要抓到，且要指出回到了**哪一轮**。"""
    state = _state(*[_snapshot(1, *sorted(item)) for item in sets])
    result = ProgressTracker().evaluate(state)
    assert result.oscillated is True
    assert result.oscillation_round == expected_round


def test_monotonic_progress_is_not_flagged_as_oscillation() -> None:
    state = _state(
        _snapshot(3, "A"),
        _snapshot(2, "B"),
        _snapshot(1, "C"),
    )
    result = ProgressTracker().evaluate(state)
    assert result.oscillated is False
    assert result.no_progress_streak == 0


def test_oscillation_overrides_apparent_signature_change() -> None:
    """B→A 单看 made_progress 是"推进"，但 A 在更早出现过，那是振荡。"""
    state = _state(_snapshot(2, "A"), _snapshot(2, "B"), _snapshot(2, "A"))
    result = ProgressTracker().evaluate(state)
    assert result.oscillated is True
    assert result.made_progress is False


def test_order_of_signatures_does_not_matter() -> None:
    """测试执行顺序变了不该被当成"出现了新的失败集合"。"""
    first = _snapshot(2, "A", "B")
    second = _snapshot(2, "B", "A")
    assert made_progress(first, second) is False
    state = _state(first, second)
    result = ProgressTracker().evaluate(state)
    assert result.no_progress_streak == 1


def test_two_consecutive_no_progress_rounds_trigger_the_stop_condition() -> None:
    state = _state(_snapshot(1, "A"), _snapshot(2, "B"), _snapshot(3, "C"))
    result = ProgressTracker().evaluate(state)
    assert result.no_progress_streak == 2
    assert result.is_no_progress is True
    # 失败集合两两不同，因此这不是 repeated_failure。
    assert result.repeated_failure is False


def test_first_round_without_baseline_is_not_counted_as_stalled() -> None:
    """首轮没有比较基线。把它记成"无进展"会让任何单轮 run 都被判成停滞。"""
    state = _state(_snapshot(1, "A"))
    result = ProgressTracker().evaluate(state)
    assert result.no_progress_streak == 0
    assert result.is_no_progress is False
    assert any("no baseline" in item for item in result.reasons)


def test_empty_signature_set_cannot_oscillate() -> None:
    """全通过的轮次签名集合为空：空集合在更早轮次出现过不算振荡。"""
    state = _state(_snapshot(0), _snapshot(1, "A"), _snapshot(0))
    result = ProgressTracker().evaluate(state)
    assert result.oscillated is False


# ------------------------------------------------------------------ DA-010 碰撞


def _parsed(test_id: str, message: str, *, file: str = "src/pkg/core.py") -> ParsedFailure:
    frame = TraceFrame(file=file, line=2, function="normalize", in_repo=True)
    return ParsedFailure(
        test_id=test_id,
        exception_type="AssertionError",
        message=message,
        frames=[frame],
        deepest_repo_frame=frame,
        category=FailureCategory.ASSERTION,
    )


def test_frozen_signature_collapses_numbers_and_long_strings() -> None:
    """复现 DA-010：两个**不同**的失败在冻结的 SHA1 下得到同一个签名。

    这条断言的意义是"碰撞确实存在"，因此它同时也是防止有人偷偷改算法让问题
    消失的锚点。
    """
    first = failure_signature(_parsed("tests/test_a.py::test_x", "assert 11 == 22"))
    second = failure_signature(_parsed("tests/test_a.py::test_x", "assert 333 == 4444"))
    assert first == second


def test_repeated_failure_requires_matching_test_identity_not_only_signature() -> None:
    """签名相同但失败的测试不同 → 疑似碰撞，不能当同一根因停止。"""
    colliding = _parsed("tests/test_a.py::test_x", "assert 11 == 22")
    other = _parsed("tests/test_b.py::test_y", "assert 99 == 100")
    assert failure_signature(colliding) == failure_signature(other)

    first = Attempt(
        iteration=1,
        patch_hash="h1",
        validation=ValidationResult(passed=True),
        execution=_execution_with([(colliding, "assert 11 == 22")]),
        failure_signatures=[failure_signature(colliding)],
        progress=_snapshot(1, failure_signature(colliding)),
    )
    second = Attempt(
        iteration=2,
        patch_hash="h2",
        validation=ValidationResult(passed=True),
        execution=_execution_with([(other, "assert 99 == 100")]),
        failure_signatures=[failure_signature(other)],
        progress=_snapshot(1, failure_signature(other)),
    )
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
        attempts=[first, second],
    )
    result = ProgressTracker().evaluate(state)
    assert result.repeated_failure is False
    assert result.signature_collision_suspected is True


def _execution_with(pairs: list[tuple[ParsedFailure, str]]) -> ExecutionResult:
    cases = [
        TestCaseResult(
            node_id=failure.test_id,
            outcome=TestOutcome.FAILED,
            duration=0.1,
            failure_type="AssertionError",
            message=message,
        )
        for failure, message in pairs
    ]
    return ExecutionResult(
        status=ExecutionStatus.FAIL,
        tests=cases,
        tests_failed=len(cases),
        artifact_dir="attempts/01",
    )


def test_round_evidence_recovers_identities_without_new_schema_fields() -> None:
    """身份核验用 Attempt.execution 里已有的 TestCaseResult，不另存一份字段。"""
    failure = _parsed("tests/test_a.py::test_x", "assert 1 == 2")
    attempt = Attempt(
        iteration=1,
        patch_hash="h1",
        validation=ValidationResult(passed=True),
        execution=_execution_with([(failure, "assert 1 == 2")]),
        failure_signatures=[failure_signature(failure)],
    )
    evidence = round_evidence(attempt)
    assert evidence is not None
    assert evidence.test_ids == frozenset({"tests/test_a.py::test_x"})
    assert evidence.categories == frozenset({"AssertionError"})
    assert evidence.signatures == frozenset({failure_signature(failure)})


def test_round_evidence_synthesises_an_identity_for_suite_level_failures() -> None:
    """超时 / 收集错误没有逐用例记录，但仍然必须能被区分与核验。"""
    attempt = Attempt(
        iteration=1,
        patch_hash="h1",
        validation=ValidationResult(passed=True),
        execution=ExecutionResult(status=ExecutionStatus.TIMEOUT, error_reason="EXECUTION_TIMEOUT"),
        failure_signatures=["t"],
    )
    evidence = round_evidence(attempt)
    assert evidence is not None
    assert "<execution:TIMEOUT>" in evidence.test_ids


def test_signature_sets_preserve_round_order() -> None:
    state = _state(_snapshot(1, "B", "A"), _snapshot(1, "C"))
    assert state.signature_sets() == [frozenset({"A", "B"}), frozenset({"C"})]


def test_tracker_ignores_attempts_without_progress_snapshots() -> None:
    """校验失败的轮次没有执行也没有快照；它们不能让历史错位。"""
    state = _state(None, _snapshot(1, "A"), _snapshot(1, "A"))
    result = ProgressTracker().evaluate(state)
    assert result.repeated_failure is True
