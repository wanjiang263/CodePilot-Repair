"""G-08：停止规则与 Patch hash 安全。

``canonical_patch`` 是这一组里最容易被"顺手简化"的地方：docs/02 §15.5 写的是
"去掉空白差异后计算"，而 Python 里 **缩进是语法、三引号字符串的行尾空格是数据**。
按字面把 diff 的所有空白抹掉，会让"换了缩进又交一份坏代码"和"改了字符串里的
空格"都得到同一个哈希，于是 ``repeated_patch`` 在错误的时间点触发。

这里的负控制用例专门盯这两种情况。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codepilot.graph.budget import BudgetManager
from codepilot.graph.stop_rules import StopRules, canonical_patch, patch_hash
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import (
    ExecutionStatus,
    RepairStrategy,
    RunStatus,
    StopReason,
    TestOutcome,
)
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.usage import Usage
from codepilot.schemas.validation import ValidationResult

DIFF_A = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "index 1234567..89abcde 100644\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value\n"
)


def _attempt(
    iteration: int,
    *,
    diff: str = DIFF_A,
    passed: bool = True,
    status: ExecutionStatus = ExecutionStatus.FAIL,
    signatures: tuple[str, ...] = ("sig-a",),
    strategy: RepairStrategy | None = None,
    is_patch_caused: bool = False,
    confidence: float = 0.5,
) -> Attempt:
    execution = (
        None
        if status is not ExecutionStatus.FAIL and not signatures
        else ExecutionResult(
            status=status,
            tests=[
                TestCaseResult(
                    node_id=f"tests/test_core.py::test_{index}",
                    outcome=TestOutcome.FAILED,
                    duration=0.1,
                    failure_type="AssertionError",
                    message=f"failure {index}",
                )
                for index in range(max(len(signatures), 1))
            ],
            tests_failed=len(signatures),
            artifact_dir=f"attempts/{iteration:02d}",
            error_reason=None if status is ExecutionStatus.FAIL else status.value,
        )
    )
    from codepilot.schemas.progress import ProgressSnapshot

    diagnosis = (
        None
        if strategy is None
        else Diagnosis(
            root_cause="hypothesis",
            repair_strategy=strategy,
            is_patch_caused=is_patch_caused,
            confidence=confidence,
        )
    )
    return Attempt(
        iteration=iteration,
        patch_diff=diff,
        patch_hash=patch_hash(diff),
        validation=ValidationResult(passed=passed),
        execution=execution,
        failure_signatures=list(signatures),
        diagnosis=diagnosis,
        progress=ProgressSnapshot(
            target_failed=len(signatures),
            regression_new_failures=0,
            signatures=frozenset(signatures),
            regression_measured=False,
        ),
    )


def _state(*attempts: Attempt, **updates) -> RepairState:
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        # started_at 取"现在"：StopRules 用的是真实时钟，钉死在过去的话每个用例
        # 都会先撞上 budget_time，看起来像是停止规则错了。
        started_at=datetime.now(UTC),
    )
    state.attempts.extend(attempts)
    return state.model_copy(update=updates) if updates else state


# ------------------------------------------------------------------ patch hash


def test_canonical_patch_drops_only_content_free_git_metadata() -> None:
    canonical = canonical_patch(DIFF_A)
    assert "index 1234567..89abcde" not in canonical
    assert "--- a/src/pkg/core.py" not in canonical
    assert "+++ b/src/pkg/core.py" not in canonical
    # 文件身份、hunk 头与正文逐字保留。
    assert "diff --git a/src/pkg/core.py b/src/pkg/core.py" in canonical
    assert "@@ -1,2 +1,2 @@" in canonical
    assert "-    return value - 1" in canonical


def test_reindentation_is_a_different_patch() -> None:
    """负控制一：只改缩进就是改语义，绝不能被当成同一份补丁。"""
    reindented = DIFF_A.replace("+    return value", "+        return value")
    assert patch_hash(reindented) != patch_hash(DIFF_A)


def test_whitespace_inside_a_string_literal_is_a_different_patch() -> None:
    """负控制二：三引号字符串里的行尾空格是数据，不是格式噪声。"""
    with_space = DIFF_A.replace("+    return value", "+    return value  ")
    assert patch_hash(with_space) != patch_hash(DIFF_A)


def test_missing_final_newline_is_not_treated_as_formatting_noise() -> None:
    """``\\ No newline at end of file`` 代表真实的文件内容差异。"""
    with_marker = DIFF_A + "\\ No newline at end of file\n"
    assert patch_hash(with_marker) != patch_hash(DIFF_A)
    assert "!NOEOL!" in canonical_patch(with_marker)


def test_line_ending_and_index_sha_differences_do_not_change_identity() -> None:
    """CRLF 与 blob 元数据变了但内容没变 → 同一补丁（这才是"忽略无意义差异"）。"""
    crlf = DIFF_A.replace("\n", "\r\n")
    other_shas = DIFF_A.replace("1234567..89abcde", "aaaaaaa..bbbbbbb")
    assert patch_hash(crlf) == patch_hash(DIFF_A)
    assert patch_hash(other_shas) == patch_hash(DIFF_A)


def test_empty_patch_has_a_stable_hash() -> None:
    assert patch_hash("") == patch_hash("")
    assert patch_hash("x") != patch_hash("")


def test_file_identity_change_is_a_different_patch() -> None:
    other = DIFF_A.replace("src/pkg/core.py", "src/pkg/other.py")
    assert patch_hash(other) != patch_hash(DIFF_A)


# ------------------------------------------------------------------ 各停止条件


def _rules() -> StopRules:
    return StopRules(budget=BudgetManager())


def test_repeated_patch_fires_against_an_earlier_round() -> None:
    state = _state(_attempt(1), _attempt(2))
    decision = _rules().evaluate(state)
    assert decision.primary is StopReason.REPEATED_PATCH
    assert "patch identical to iteration 1" in decision.explanations


def test_a_patch_may_always_be_compared_only_to_earlier_rounds() -> None:
    """刚追加的当前轮不能把自己算成重复。"""
    assert _rules().evaluate(_state(_attempt(1))).primary is not StopReason.REPEATED_PATCH


def test_repeated_failure_needs_two_identical_rounds() -> None:
    first = _attempt(1, diff=DIFF_A, signatures=("sig-a",))
    second = _attempt(2, diff="diff --git a/x.py b/x.py\n+q\n", signatures=("sig-a",))
    state = _state(first, second)
    decision = _rules().evaluate(state)
    assert StopReason.REPEATED_FAILURE in decision.all_reasons


def test_different_failures_never_trigger_repeated_failure() -> None:
    first = _attempt(1, signatures=("sig-a",))
    second = _attempt(2, diff="diff --git a/y.py b/y.py\n+z\n", signatures=("sig-b",))
    decision = _rules().evaluate(_state(first, second))
    assert StopReason.REPEATED_FAILURE not in decision.all_reasons


def test_validation_loop_needs_three_consecutive_failures() -> None:
    assert _rules().consecutive_validation_failures(_state(_attempt(1, passed=False))) == 1
    two = _state(_attempt(1, passed=False), _attempt(2, passed=False))
    assert StopReason.VALIDATION_LOOP not in _rules().evaluate(two).all_reasons
    three = _state(
        _attempt(1, passed=False),
        _attempt(2, passed=False, diff="diff --git a/z.py b/z.py\n+q\n"),
        _attempt(3, passed=False, diff="diff --git a/w.py b/w.py\n+r\n"),
    )
    decision = _rules().evaluate(three)
    assert StopReason.VALIDATION_LOOP in decision.all_reasons
    assert decision.primary is StopReason.VALIDATION_LOOP


def test_a_passed_round_resets_the_validation_streak() -> None:
    state = _state(
        _attempt(1, passed=False),
        _attempt(2, passed=True, diff="diff --git a/q.py b/q.py\n+s\n"),
        _attempt(3, passed=False, diff="diff --git a/r.py b/r.py\n+t\n"),
    )
    assert _rules().consecutive_validation_failures(state) == 1


def test_sandbox_and_environment_errors_stop_immediately() -> None:
    for status in (ExecutionStatus.ENV_ERROR, ExecutionStatus.SANDBOX_ERROR):
        execution = ExecutionResult(status=status, error_reason=status.value)
        state = _state(_attempt(1, status=status), execution=execution)
        decision = _rules().evaluate(state)
        assert decision.primary is StopReason.SANDBOX_FAILURE
        assert _rules().status_for(decision) is RunStatus.SANDBOX_ERROR


def test_oom_is_covered_by_the_sandbox_branch_without_special_handling() -> None:
    execution = ExecutionResult(
        status=ExecutionStatus.SANDBOX_ERROR,
        error_reason="OOM_KILLED",
        diagnostics={"oom_killed": True},
    )
    state = _state(_attempt(1, status=ExecutionStatus.SANDBOX_ERROR), execution=execution)
    assert _rules().evaluate(state).primary is StopReason.SANDBOX_FAILURE


def test_diagnosis_give_up_stops_the_run() -> None:
    state = _state(_attempt(1, strategy=RepairStrategy.GIVE_UP))
    decision = _rules().evaluate(state)
    assert decision.primary is StopReason.DIAGNOSIS_GIVE_UP
    assert _rules().status_for(decision) is RunStatus.STOPPED


def test_give_up_is_not_influenced_by_confidence() -> None:
    """confidence 只进日志：0.0 与 1.0 的 give_up 必须给出同一个停止决定。"""
    for confidence in (0.0, 0.4, 1.0):
        state = _state(_attempt(1, strategy=RepairStrategy.GIVE_UP, confidence=confidence))
        assert _rules().evaluate(state).primary is StopReason.DIAGNOSIS_GIVE_UP


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("test_runs", 99, StopReason.BUDGET_TEST_RUNS),
        ("usage", Usage(input_tokens=10**9, output_tokens=0), StopReason.BUDGET_TOKENS),
        ("usage", Usage(input_tokens=0, output_tokens=0, cost=10.0), StopReason.BUDGET_COST),
    ],
)
def test_budget_exhaustion_is_reported_as_a_stop_reason(
    field: str, value: object, expected: StopReason
) -> None:
    budget = RepairBudget(max_cost=5.0) if expected is StopReason.BUDGET_COST else RepairBudget()
    state = _state(_attempt(1, signatures=()), budget=budget, **{field: value})
    decision = _rules().evaluate(state)
    assert expected in decision.all_reasons
    assert _rules().status_for(decision) is RunStatus.BUDGET_EXHAUSTED


def test_no_budget_reason_when_under_every_limit() -> None:
    state = _state(_attempt(1, signatures=()), test_runs=1)
    decision = _rules().evaluate(state)
    assert not [reason for reason in decision.all_reasons if reason.value.startswith("budget_")]


def test_priority_reports_environment_before_budget_and_repetition() -> None:
    """环境不可信时，其他信号都是从坏运行里读出来的，primary 必须是它。"""
    execution = ExecutionResult(status=ExecutionStatus.SANDBOX_ERROR, error_reason="boom")
    state = _state(_attempt(1), _attempt(2), test_runs=99, execution=execution)
    decision = _rules().evaluate(state)
    assert decision.primary is StopReason.SANDBOX_FAILURE
    # 但不隐藏其他命中项：报告要能看出"同时也超了测试预算、补丁也重复了"。
    assert StopReason.BUDGET_TEST_RUNS in decision.all_reasons
    assert StopReason.REPEATED_PATCH in decision.all_reasons


def test_decision_without_any_condition_keeps_running() -> None:
    decision = _rules().evaluate(_state(_attempt(1, signatures=("a",))))
    assert decision.should_stop is False
    assert decision.primary is None
    assert _rules().status_for(decision) is None


def test_repeated_and_no_progress_can_coexist_without_hiding_either() -> None:
    """两个条件同时成立时 primary 选一个，但 all_reasons 必须都留着。"""
    first = _attempt(1, signatures=("sig-a",))
    second = _attempt(2, diff="diff --git a/v.py b/v.py\n+v\n", signatures=("sig-a",))
    third = _attempt(3, diff="diff --git a/u.py b/u.py\n+u\n", signatures=("sig-a",))
    decision = _rules().evaluate(_state(first, second, third))
    assert StopReason.REPEATED_FAILURE in decision.all_reasons
    assert StopReason.NO_PROGRESS in decision.all_reasons
    assert decision.primary is StopReason.REPEATED_FAILURE
