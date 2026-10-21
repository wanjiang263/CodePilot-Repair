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
    pending_review_calls,
    reproduction_target_tests,
    review_preconditions,
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
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.regression import RegressionResult
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

    state = _state(
        attempts=[_attempt()],
        regression=RegressionResult(new_failures=[], complete=True, status="completed"),
    )
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


# ------------------------------------------------------- Phase 11：闭环状态转移


def _closed(**updates) -> RepairState:
    """一个"补丁已批准、L1 通过、回归干净"的可审查现场。"""
    base = {
        "attempts": [_attempt(execution=_execution(ExecutionStatus.PASS))],
        "execution": _execution(ExecutionStatus.PASS),
        "regression": RegressionResult(complete=True),
        "issue_analysis": IssueAnalysis(summary="x"),
    }
    base.update(updates)
    return _state(**base)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({}, None),
        ({"attempts": []}, "no attempt"),
        ({"attempts": [_attempt(passed=False)]}, "not approved by PatchValidator"),
        ({"execution": None}, "target tests were never executed"),
        (
            {"execution": _execution(ExecutionStatus.FAIL)},
            "target tests ended as FAIL",
        ),
        ({"regression": None}, "regression was not measured"),
        ({"regression": RegressionResult(complete=False)}, "regression is incomplete"),
        (
            {"regression": RegressionResult(complete=True, new_failures=["a::b"])},
            "confirmed new failures",
        ),
        ({"issue_analysis": None}, "no issue analysis"),
    ],
)
def test_review_preconditions_table(updates: dict, expected: str | None) -> None:
    """§四/§五：审查只在真实证据齐备时调用；每条不满足都给出**具体**原因。"""
    reason = review_preconditions(_closed(**updates))
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_a_local_coding_stop_with_a_patch_is_not_a_terminal_state() -> None:
    """场景 A 的路由面：局部额度写进 ``coding_stop_reason`` 之后，图必须继续往下走。"""
    state = _state(
        attempts=[_attempt()],
        coding_stop_reason=StopReason.BUDGET_TOOL_CALLS,
        plan=RepairPlan(
            root_cause_hypothesis="x", alternatives=[], tests_to_run=["tests/test_core.py"]
        ),
    )
    assert terminated(state) is False
    assert route_after_rules(state, Node.VALIDATE_PATCH) is Node.VALIDATE_PATCH
    assert route_after_validate_patch(state) is Node.RUN_TARGET_TESTS


def test_a_local_coding_stop_without_target_tests_still_reaches_regression() -> None:
    """没有目标测试时也不得直奔 finalize：回归边界照走，成功判定由 finalize 保守处理。"""
    state = _state(
        attempts=[_attempt()],
        coding_stop_reason=StopReason.BUDGET_COST,
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )
    assert route_after_validate_patch(state) is Node.RUN_REGRESSION


def test_a_local_coding_stop_without_a_patch_is_terminal() -> None:
    """场景 B 的路由面：没有补丁时这才是真终态，短路到 finalize。"""
    state = _state(
        status=RunStatus.BUDGET_EXHAUSTED,
        stop_reason=StopReason.BUDGET_TOOL_CALLS,
        coding_stop_reason=StopReason.BUDGET_TOOL_CALLS,
    )
    assert route_after_rules(state, Node.VALIDATE_PATCH) is Node.FINALIZE
    assert route_after_validate_patch(state) is Node.FINALIZE


def test_failed_target_tests_after_a_local_stop_still_reach_diagnosis() -> None:
    """场景 C：局部额度耗尽之后测试失败，仍然进 Failure Diagnosis 而不是终止在编码。"""
    state = _state(
        attempts=[_attempt(execution=_execution(ExecutionStatus.FAIL))],
        execution=_execution(ExecutionStatus.FAIL),
        coding_stop_reason=StopReason.BUDGET_TOOL_CALLS,
    )
    assert route_after_target_tests(state) is Node.DIAGNOSE
    assert route_after_diagnose(state) is Node.CHECK_BUDGET


def test_second_high_review_after_one_repair_stops_instead_of_looping() -> None:
    """场景 D：第二次 high 不再进修复环——修复额度是 1，循环必须有上限。"""
    high = ReviewResult(
        approved=False, issues=[ReviewIssue(severity=ReviewSeverity.HIGH, file="a.py")]
    )
    assert route_after_review(_closed(review=high)) is Node.CHECK_BUDGET
    assert route_after_review(_closed(review=high, review_repairs=1)) is Node.FINALIZE


@pytest.mark.parametrize(
    "route",
    [
        lambda state: route_after_validate_patch(state),
        lambda state: route_after_target_tests(state),
        lambda state: route_after_regression(state),
        lambda state: route_after_review(state),
        lambda state: route_after_diagnose(state),
        lambda state: route_after_check_budget(state, stop=False),
        lambda state: route_after_rules(state, Node.REVIEW),
    ],
)
def test_hard_budget_stop_cannot_be_routed_around(route) -> None:
    """场景 E：全局预算终态写在 state 里，任何一条边都不能把它绕回验证或审查。"""
    state = _closed(
        status=RunStatus.BUDGET_EXHAUSTED,
        stop_reason=StopReason.BUDGET_COST,
        review=None,
    )
    assert route(state) is Node.FINALIZE


def test_unmeasured_regression_still_reaches_the_review_node() -> None:
    """回归未测量时把**节点**走到，由节点判定"审查未完成"，而不是图直接跳过它。"""
    assert route_after_regression(_closed(regression=None)) is Node.REVIEW


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


# ------------------------------------------------------- 闭环预留的欠账计数


def _review(digest: str, *, high: bool = False) -> ReviewResult:
    issues = (
        [ReviewIssue(severity=ReviewSeverity.HIGH, file="src/a.py", description="boom")]
        if high
        else []
    )
    return ReviewResult(approved=not high, summary="s", issues=issues, reviewed_patch_hash=digest)


@pytest.mark.parametrize(
    ("review", "repairs", "calls", "status", "expected"),
    [
        # 编码派发时：补丁还不存在，初审 + 一次可能的复审都欠着。
        (None, 0, 0, RunStatus.RUNNING, 2),
        # 修复轮已经用掉：只剩初审这一次欠账。
        (None, 1, 0, RunStatus.RUNNING, 1),
        # 当前候选已经审过并且没有 high：闭环挣完了，预留必须归零。
        ("cur", 0, 1, RunStatus.RUNNING, 0),
        ("cur", 0, 1, RunStatus.SUCCEEDED, 0),
        # high + 还没用过修复轮 → 欠一次复审；而那轮复审之后不再有第二次机会。
        ("cur-high", 0, 1, RunStatus.RUNNING, 1),
        ("cur-high", 1, 1, RunStatus.RUNNING, 0),
        # 审查专属额度封顶：为审不成的调用留钱等于白留。
        (None, 0, 1, RunStatus.RUNNING, 1),
        (None, 0, 2, RunStatus.RUNNING, 0),
        ("cur", 0, 2, RunStatus.RUNNING, 0),
        # 审查结论属于**上一版**补丁：当前候选一次也没审过，欠账重新算满。
        ("old", 0, 1, RunStatus.RUNNING, 1),
        # run 已进终态：预留立即释放，不得永久占用预算。
        (None, 0, 0, RunStatus.BUDGET_EXHAUSTED, 0),
        (None, 0, 0, RunStatus.ERROR, 0),
        ("cur", 0, 0, RunStatus.STOPPED, 0),
    ],
)
def test_pending_review_calls_follows_the_state_machine(
    review: str | None, repairs: int, calls: int, status: RunStatus, expected: int
) -> None:
    """预留的计数口径：只数"**还欠着**的审查"，不数已经挣到或再也不可能的。

    这里刻意不带 ``review_preconditions``：那条判"此刻能不能审"，编码派发时补丁还不
    存在，用它算预留会永远得出 0，"给审查留钱"这句话就从未成立过。
    """
    digest = "candidate"
    mapping = {
        "cur": _review(digest),
        "cur-high": _review(digest, high=True),
        "old": _review("other-patch"),
    }
    state = _state(
        status=status,
        review=mapping.get(review) if review else None,
        review_repairs=repairs,
        review_calls=calls,
        current_patch="patch-text",
    )
    assert pending_review_calls(state, candidate_digest=digest) == expected
    # 没有补丁身份（编码派发之前）时，任何已存在的结论都算不上"审过当前候选"；
    # 但专属额度已经花完时仍然是 0 —— 为审不成的调用留钱没有意义。
    if review in {"cur", "cur-high"} and status is RunStatus.RUNNING and calls < 2:
        assert pending_review_calls(state, candidate_digest="") >= 1


def test_pending_review_calls_is_capped_by_the_configured_allowance() -> None:
    """``max_review_calls`` 收紧时欠账跟着收紧：政策改了，预留不能还按 2 次留。"""
    from codepilot.schemas.budget import RepairBudget

    state = _state(
        budget=RepairBudget(max_review_calls=1), current_patch="patch-text", review_calls=0
    )
    assert pending_review_calls(state, candidate_digest="candidate") == 1
    spent = state.model_copy(update={"budget": RepairBudget(max_review_calls=0)})
    assert pending_review_calls(spent, candidate_digest="candidate") == 0
