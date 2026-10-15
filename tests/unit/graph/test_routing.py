"""G-03：确定性路由的分支单测。

03 §5 要求"路由函数是纯函数且有单元测试，每个分支一个用例"。这里逐条覆盖
02 §15.2 的边表，并专门验证四条容易被含糊实现的规则：

* ``ENV_ERROR`` / ``SANDBOX_ERROR`` 直接终止，**不进**诊断循环；
* OOM 已被 Phase 4 归一成 ``SANDBOX_ERROR``，因此它天然走终止边；
* ``Diagnosis.confidence`` 不参与路由；
* 没有目标测试时进入回归/待验收边界，而不是伪造一次"目标测试通过"。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codepilot.graph.routing import (
    Node,
    reproduction_target_tests,
    route_after_check_budget,
    route_after_diagnose,
    route_after_regression,
    route_after_review,
    route_after_rules,
    route_after_target_tests,
    route_after_validate_patch,
    selected_target_tests,
    terminated,
)
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import (
    ExecutionStatus,
    RepairStrategy,
    ReviewSeverity,
    RunStatus,
    StopReason,
    TestOutcome,
)
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.reproduction import (
    ReproductionAttempt,
    ReproductionResult,
    ReproductionStatus,
)
from codepilot.schemas.review import ReviewIssue, ReviewResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult

ALL_STATUSES = list(ExecutionStatus)


def _state(**updates) -> RepairState:
    base = {
        "task_id": "t",
        "run_id": "r",
        "repository_id": "repo",
        "base_commit": "abc",
        "issue": "x",
        "started_at": datetime(2026, 10, 14, tzinfo=UTC),
    }
    base.update(updates)
    return RepairState(**base)


def _attempt(*, passed: bool = True, execution: ExecutionResult | None = None) -> Attempt:
    return Attempt(
        iteration=1,
        patch_hash="h1",
        validation=ValidationResult(passed=passed),
        execution=execution,
    )


def _execution(status: ExecutionStatus, *, error_reason: str | None = None) -> ExecutionResult:
    return ExecutionResult(status=status, error_reason=error_reason)


# ------------------------------------------------------------------ 通用短路


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.STOPPED,
        RunStatus.ERROR,
        RunStatus.PROVISIONALLY_VERIFIED,
        RunStatus.BUDGET_EXHAUSTED,
    ],
)
def test_any_terminal_status_short_circuits_to_finalize(status: RunStatus) -> None:
    state = _state(status=status)
    assert terminated(state) is True
    for router in (route_after_validate_patch, route_after_target_tests, route_after_review):
        assert router(state) is Node.FINALIZE
    assert route_after_rules(state, Node.RETRIEVE) is Node.FINALIZE
    assert route_after_regression(state) is Node.FINALIZE
    assert route_after_diagnose(state) is Node.FINALIZE
    assert route_after_check_budget(state, stop=False) is Node.FINALIZE


def test_running_state_is_not_terminal() -> None:
    assert terminated(_state()) is False


def test_route_after_rules_follows_the_declared_next_node() -> None:
    state = _state()
    assert route_after_rules(state, Node.RETRIEVE) is Node.RETRIEVE


# ------------------------------------------------------------------ validate


def test_valid_patch_with_targets_runs_target_tests() -> None:
    from codepilot.schemas.plan import RepairPlan

    state = _state(
        attempts=[_attempt()],
        plan=RepairPlan(
            root_cause_hypothesis="h", tests_to_run=["tests/test_core.py"], alternatives=[]
        ),
    )
    assert route_after_validate_patch(state) is Node.RUN_TARGET_TESTS


def test_invalid_patch_goes_to_check_budget_without_running_tests() -> None:
    """无效补丁**不执行测试**：预算要留给"连续三次校验失败"的计数。"""
    state = _state(attempts=[_attempt(passed=False)])
    assert route_after_validate_patch(state) is Node.CHECK_BUDGET


def test_valid_patch_without_targets_goes_to_the_regression_boundary() -> None:
    """没有目标测试时按 02 §15.2 直接进入回归/待验收边界，不伪造目标测试通过。"""
    from codepilot.schemas.plan import RepairPlan

    state = _state(
        attempts=[_attempt()],
        plan=RepairPlan(root_cause_hypothesis="h", tests_to_run=[], alternatives=[]),
    )
    assert route_after_validate_patch(state) is Node.RUN_REGRESSION
    assert selected_target_tests(state) == ()


def test_missing_attempt_is_rejected_not_assumed_valid() -> None:
    """校验节点没留下 Attempt：这是流程错误，不能猜它成功。"""
    assert route_after_validate_patch(_state()) is Node.FINALIZE


# ------------------------------------------------------------------ 目标测试


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ExecutionStatus.PASS, Node.RUN_REGRESSION),
        (ExecutionStatus.FAIL, Node.DIAGNOSE),
        (ExecutionStatus.TIMEOUT, Node.DIAGNOSE),
        (ExecutionStatus.COLLECTION_ERROR, Node.DIAGNOSE),
        (ExecutionStatus.ENV_ERROR, Node.FINALIZE),
        (ExecutionStatus.SANDBOX_ERROR, Node.FINALIZE),
    ],
)
def test_all_six_execution_statuses_route_per_the_table(
    status: ExecutionStatus, expected: Node
) -> None:
    state = _state(
        attempts=[_attempt(execution=_execution(status))],
        execution=_execution(status),
    )
    assert route_after_target_tests(state) is expected


def test_oom_maps_to_sandbox_error_and_cannot_enter_the_repair_loop() -> None:
    """OOM 沿用 Phase 4 的 ``SANDBOX_ERROR/OOM_KILLED``，因此必然终止。

    这里不另开一个 OOM 分支：再加一次 ``error_reason == "OOM_KILLED"`` 判断会让
    同一件事有两个口径，改协议时只改一处就会静默失效。
    """
    state = _state(
        attempts=[
            _attempt(execution=_execution(ExecutionStatus.SANDBOX_ERROR, error_reason="OOM_KILLED"))
        ],
        execution=_execution(ExecutionStatus.SANDBOX_ERROR, error_reason="OOM_KILLED"),
    )
    assert route_after_target_tests(state) is Node.FINALIZE


def test_missing_execution_result_is_not_treated_as_pass() -> None:
    state = _state(attempts=[_attempt(execution=None)])
    assert route_after_target_tests(state) is Node.FINALIZE


# ------------------------------------------------------------------ 回归 / 复审


def test_unmeasured_regression_is_not_read_as_zero_new_failures() -> None:
    """``regression is None`` 是未测量，继续走向复审，不进诊断。"""
    assert route_after_regression(_state(attempts=[_attempt()])) is Node.REVIEW


def test_regression_with_new_failures_goes_to_diagnose() -> None:
    from codepilot.schemas.regression import RegressionResult

    state = _state(
        attempts=[_attempt()], regression=RegressionResult(new_failures=["tests/test_a.py::test_x"])
    )
    assert route_after_regression(state) is Node.DIAGNOSE


def test_clean_regression_goes_to_review() -> None:
    from codepilot.schemas.regression import RegressionResult

    state = _state(attempts=[_attempt()], regression=RegressionResult(new_failures=[]))
    assert route_after_regression(state) is Node.REVIEW


def test_review_without_a_reviewer_result_finalizes() -> None:
    assert route_after_review(_state(review=None)) is Node.FINALIZE


def test_approved_review_finalizes() -> None:
    state = _state(review=ReviewResult(approved=True))
    assert route_after_review(state) is Node.FINALIZE


def test_medium_issues_do_not_trigger_an_extra_round() -> None:
    """只有 high 才会触发额外修复；medium 记录意见但按通过处理。"""
    state = _state(
        review=ReviewResult(
            approved=False, issues=[ReviewIssue(severity=ReviewSeverity.MEDIUM, file="a.py")]
        )
    )
    assert route_after_review(state) is Node.FINALIZE


def test_high_issues_return_to_check_budget() -> None:
    state = _state(
        review=ReviewResult(
            approved=False, issues=[ReviewIssue(severity=ReviewSeverity.HIGH, file="a.py")]
        )
    )
    assert route_after_review(state) is Node.CHECK_BUDGET


# ------------------------------------------------------------------ 诊断 / 预算


def test_diagnose_always_flows_into_check_budget() -> None:
    assert route_after_diagnose(_state(attempts=[_attempt()])) is Node.CHECK_BUDGET


@pytest.mark.parametrize(
    "confidence",
    [0.0, 0.2, 0.55, 1.0],
)
def test_diagnosis_confidence_never_changes_routing(confidence: float) -> None:
    """confidence 只用于分析（02 §14.6）；路由对它的四个典型取值给同一个答案。"""
    diagnosis = Diagnosis(root_cause="hypothesis", confidence=confidence)
    state = _state(
        attempts=[_attempt().model_copy(update={"diagnosis": diagnosis})],
        execution=_execution(ExecutionStatus.FAIL),
    )
    assert route_after_diagnose(state) is Node.CHECK_BUDGET
    assert route_after_target_tests(state) is Node.DIAGNOSE


def test_check_budget_stop_flag_decides_the_branch() -> None:
    state = _state(attempts=[_attempt()])
    assert route_after_check_budget(state, stop=True) is Node.FINALIZE
    assert route_after_check_budget(state, stop=False) is Node.REPAIR_PATCH


def test_model_strategy_cannot_bypass_budget_or_sandbox_termination() -> None:
    """策略与状态字段都指向"继续修"时，预算/环境终止信号仍然优先。"""
    stopped = _state(
        attempts=[_attempt()], stop_reason=StopReason.BUDGET_ITERATIONS, status=RunStatus.STOPPED
    )
    assert route_after_check_budget(stopped, stop=True) is Node.FINALIZE
    env = _state(execution=_execution(ExecutionStatus.ENV_ERROR))
    assert route_after_target_tests(env) is Node.FINALIZE


# ------------------------------------------------------------------ 目标测试口径


def test_reproduced_target_tests_take_priority_over_the_plan() -> None:
    from codepilot.schemas.plan import RepairPlan

    reproduction = ReproductionResult(
        status=ReproductionStatus.REPRODUCED,
        test_path="tests/_codepilot_repro/test_repro.py",
        fail_reason="assert 2 == 3",
        attempts=1,
        kind="valid_assertion",
        history=[
            ReproductionAttempt(
                index=1,
                kind="valid_assertion",
                detail="assert 2 == 3",
                test_path="tests/_codepilot_repro/test_repro.py",
                node_id="tests/_codepilot_repro/test_repro.py::test_repro",
            )
        ],
    )
    state = _state(
        reproduction=reproduction,
        plan=RepairPlan(
            root_cause_hypothesis="h", tests_to_run=["tests/test_core.py"], alternatives=[]
        ),
    )
    assert selected_target_tests(state) == ("tests/_codepilot_repro/test_repro.py::test_repro",)


def test_rejected_reproduction_candidates_are_not_used_as_targets() -> None:
    """history 里被否决的候选也带 node_id，只有最终 test_path 那一条算目标。"""
    reproduction = ReproductionResult(
        status=ReproductionStatus.REPRODUCED,
        test_path="tests/_codepilot_repro/test_second.py",
        fail_reason="assert 2 == 3",
        attempts=2,
        kind="valid_assertion",
        history=[
            ReproductionAttempt(
                index=1,
                kind="invalid_pass",
                detail="passed on base",
                test_path="tests/_codepilot_repro/test_first.py",
                node_id="tests/_codepilot_repro/test_first.py::test_a",
            ),
            ReproductionAttempt(
                index=2,
                kind="valid_assertion",
                detail="assert 2 == 3",
                test_path="tests/_codepilot_repro/test_second.py",
                node_id="tests/_codepilot_repro/test_second.py::test_b",
            ),
        ],
    )
    assert reproduction_target_tests(_state(reproduction=reproduction)) == (
        "tests/_codepilot_repro/test_second.py::test_b",
    )


def test_failed_reproduction_degrades_to_planner_tests() -> None:
    from codepilot.schemas.plan import RepairPlan

    reproduction = ReproductionResult(
        status=ReproductionStatus.FAILED,
        fail_reason="retries exhausted",
        attempts=4,
        kind="invalid_pass",
        history=[
            ReproductionAttempt(index=index, kind="invalid_pass", detail="passed on base")
            for index in range(1, 5)
        ],
    )
    state = _state(
        reproduction=reproduction,
        plan=RepairPlan(
            root_cause_hypothesis="h", tests_to_run=["tests/test_core.py"], alternatives=[]
        ),
    )
    assert selected_target_tests(state) == ("tests/test_core.py",)


def test_strategy_enum_does_not_open_a_routing_shortcut() -> None:
    """RepairStrategy 出现在 diagnosis 里也不能改变图的结构边。"""
    for strategy in RepairStrategy:
        diagnosis = Diagnosis(root_cause="hypothesis", repair_strategy=strategy)
        state = _state(attempts=[_attempt().model_copy(update={"diagnosis": diagnosis})])
        assert route_after_diagnose(state) is Node.CHECK_BUDGET


def test_outcome_objects_stay_consistent_with_the_status_enum() -> None:
    """防呆：路由表按六态穷举，枚举扩容时这里先失败，提示补齐路由。"""
    assert {status.value for status in ALL_STATUSES} == {
        "PASS",
        "FAIL",
        "TIMEOUT",
        "COLLECTION_ERROR",
        "ENV_ERROR",
        "SANDBOX_ERROR",
    }
    assert TestOutcome.PASSED.value == "passed"
