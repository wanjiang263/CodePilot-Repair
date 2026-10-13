"""04 Phase 0 验收：``RepairState`` JSON 序列化后完整还原（round-trip）。

这个测试看似多余，但它挡住的是最贵的一类 bug：状态里某个字段在序列化时
被静默丢弃（例如 ``frozenset`` 或嵌套模型被写成字符串），checkpoint 恢复后
行为改变，而单元测试全绿。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codepilot.schemas import (
    Attempt,
    Diagnosis,
    Evidence,
    EvidenceKind,
    ExecutionResult,
    ExecutionStatus,
    FailureCategory,
    IssueAnalysis,
    ProgressSnapshot,
    RepairBudget,
    RepairPlan,
    RepairState,
    ReproductionAttempt,
    ReproductionFailureKind,
    ReproductionResult,
    ReproductionStatus,
    RetrievalQuery,
    RetrievalResult,
    ReviewIssue,
    ReviewResult,
    ReviewSeverity,
    RunStatus,
    StopReason,
    TaskMode,
    TestBaseline,
    TestCaseResult,
    TestOutcome,
    TraceFrame,
    Usage,
    ValidationResult,
)


def _populated_state() -> RepairState:
    """构造一个**每个字段都非默认值**的状态。

    只填少数几个字段的 round-trip 测试几乎没有价值——默认值本来就能往返。
    """
    return RepairState(
        task_id="synthetic/off_by_one_03",
        run_id="run_20261008_001",
        repository_id="repo_abc",
        base_commit="deadbeef",
        issue="IndexError when items is empty",
        mode=TaskMode.SYNTHETIC,
        issue_analysis=IssueAnalysis(
            summary="空列表访问下标",
            error_messages=["IndexError: list index out of range"],
            traceback_frames=[
                TraceFrame(
                    file="src/pkg/core.py",
                    line=42,
                    function="first",
                    in_repo=True,
                    code="return items[0]",
                )
            ],
            keywords=["IndexError", "empty"],
        ),
        retrieval=RetrievalResult(
            query=RetrievalQuery(raw_issue="IndexError", identifiers=["first"]),
            snapshot_id="snap_001",
        ),
        baseline=TestBaseline(
            base_commit="deadbeef",
            outcomes={"tests/test_core.py::test_first": TestOutcome.PASSED},
            flaky={"tests/test_core.py::test_flaky"},
        ),
        reproduction=ReproductionResult(
            status=ReproductionStatus.REPRODUCED,
            test_path="tests/_codepilot_repro/test_repro.py",
            fail_reason="IndexError",
            attempts=2,
            kind=ReproductionFailureKind.VALID_ISSUE_EXCEPTION,
            history=[
                ReproductionAttempt(
                    index=1,
                    kind=ReproductionFailureKind.INVALID_PASS,
                    detail="复现测试在 base 上通过",
                    test_path="tests/_codepilot_repro/test_repro.py",
                ),
                ReproductionAttempt(
                    index=2,
                    kind=ReproductionFailureKind.VALID_ISSUE_EXCEPTION,
                    detail="IndexError: list index out of range",
                    test_path="tests/_codepilot_repro/test_repro.py",
                    node_id="tests/_codepilot_repro/test_repro.py::test_first",
                    failure_type="IndexError",
                    exit_code=1,
                ),
            ],
        ),
        plan=RepairPlan(
            root_cause_hypothesis="未处理空列表",
            alternatives=["改为返回默认值"],
            tests_to_run=["tests/test_core.py"],
        ),
        attempts=[
            Attempt(
                iteration=1,
                patch_diff="--- a/src/pkg/core.py\n+++ b/src/pkg/core.py\n",
                patch_hash="hash_1",
                validation=ValidationResult(
                    passed=True, files_changed=1, lines_added=2, lines_deleted=1
                ),
                execution=ExecutionResult(
                    status=ExecutionStatus.FAIL,
                    exit_code=1,
                    duration_seconds=4.2,
                    tests=[
                        TestCaseResult(
                            node_id="tests/test_core.py::test_first",
                            outcome=TestOutcome.FAILED,
                            duration=0.1,
                            message="IndexError",
                        )
                    ],
                    tests_passed=3,
                    tests_failed=1,
                ),
                failure_signatures=["sig_a"],
                diagnosis=Diagnosis(
                    failure_category=FailureCategory.VALUE_KEY_INDEX,
                    root_cause="仍未处理空列表",
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.CODE,
                            file="src/pkg/core.py",
                            line=42,
                            text="return items[0]",
                        )
                    ],
                    confidence=0.6,
                ),
                progress=ProgressSnapshot(
                    target_failed=1,
                    regression_new_failures=0,
                    signatures=frozenset({"sig_a"}),
                ),
            )
        ],
        current_patch="--- a\n+++ b\n",
        review=ReviewResult(
            approved=False,
            issues=[
                ReviewIssue(
                    severity=ReviewSeverity.HIGH,
                    file="src/pkg/core.py",
                    line=42,
                    description="只处理了 list 未处理 tuple",
                )
            ],
            summary="覆盖不全",
        ),
        budget=RepairBudget(max_iterations=3, max_cost=12.5, budget_currency="CNY"),
        usage=Usage(input_tokens=1200, output_tokens=340, cost=0.42, llm_calls=5),
        test_runs=4,
        started_at=datetime(2026, 10, 8, 15, 20, 31, 402000, tzinfo=UTC),
        status=RunStatus.STOPPED,
        stop_reason=StopReason.NO_PROGRESS,
    )


def test_repair_state_roundtrips_through_json() -> None:
    """04 Phase 0 验收条目。"""
    state = _populated_state()
    restored = RepairState.model_validate_json(state.model_dump_json())
    assert restored == state


def test_roundtrip_preserves_frozenset_and_set_fields() -> None:
    """``frozenset`` / ``set`` 是最容易在 JSON 往返中丢类型的字段。

    它们退化成 list 之后 ``==`` 仍然为真，所以上一条测试可能漏掉；
    这里直接断言类型。
    """
    state = _populated_state()
    restored = RepairState.model_validate_json(state.model_dump_json())

    sigs = restored.attempts[0].progress
    assert sigs is not None
    assert isinstance(sigs.signatures, frozenset)

    assert restored.baseline is not None
    assert isinstance(restored.baseline.flaky, set)


def test_roundtrip_is_stable_across_repeated_dumps() -> None:
    """两次 dump 必须产生完全相同的文本。

    否则 checkpoint 的 diff 会充满假变更，``patch_hash`` 类的比较也不可靠。
    """
    state = _populated_state()
    assert state.model_dump_json() == state.model_dump_json()


def test_default_state_is_minimal_but_valid() -> None:
    """只给必填字段也能构造——图测试大量依赖这一点。"""
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert state.status is RunStatus.RUNNING
    assert state.attempts == []
    assert state.stop_reason is None
    assert state.iteration == 0
    assert state.current_attempt is None
    assert state.budget.max_iterations == 5


def test_usage_defaults_are_not_shared_between_instances() -> None:
    """可变默认值必须逐实例创建，否则两个 state 会共享同一个 ``Usage``。"""
    a = RepairState(
        task_id="a",
        run_id="ra",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = RepairState(
        task_id="b",
        run_id="rb",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    a.usage.add_call(alias="m", input_tokens=10, output_tokens=1, cost=0.01)
    assert b.usage.llm_calls == 0
    assert b.usage.total_tokens == 0


@pytest.mark.parametrize("reason", list(StopReason))
def test_every_stop_reason_serializes(reason: StopReason) -> None:
    """停止原因写进数据库和报告，每一个取值都必须能往返。"""
    assert StopReason(reason.value) is reason
