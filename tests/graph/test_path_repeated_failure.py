"""G-17：连续两轮失败签名集合一致 → ``repeated_failure``。

同一条路径还要带一个**风险负控制**（DA-010）：两个真正不同的故障可能被冻结的
SHA1 归成同一个签名。这里构造"签名相同、失败测试身份不同"的两轮，断言系统
**不**据此停止 —— 误判同一根因会让一个还能修的 run 提前结束，比多花一轮严重。

补丁必须两轮不同，否则 ``repeated_patch`` 会先命中（优先级在 ``repeated_failure``
之前），测不到想要的那条边。所以第二轮走 ``revert_and_retry``，并额外准备一次
重新规划的 LLM 脚本。
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness, diagnosis, passed_result, scripted_result

from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult


def _failure(node_id: str, message: str) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=node_id,
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type="AssertionError",
                message=message,
                traceback=f"src/pkg/core.py:2: AssertionError: {message}",
            )
        ],
        exit_code=1,
    )


def _edit(search: str, replace: str) -> dict[str, object]:
    return {
        "tool": "edit_file",
        "arguments": {"path": "src/pkg/core.py", "search": search, "replace": replace},
    }


NODE = "tests/test_core.py::test_normalize"


def test_identical_failure_signature_set_stops_the_loop(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[
            # 两轮原始失败消息一致；不同消息的碰撞由 Phase 9 负控制覆盖。
            _failure(NODE, "assert 1 == 3"),
            _failure(NODE, "assert 1 == 3"),
        ],
        coder_rounds=[
            [_edit("    return value - 1", "    return value - 2"), FINISH],
            [_edit("    return value - 1", "    return value - 3"), FINISH],
        ],
        # 换策略回退重做，保证两轮补丁不同（否则先命中 repeated_patch）。
        # 第二轮也要诊断：diagnose 在 check_budget 之前，停止判定发生在它后面。
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
        extra_plans=1,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.REPEATED_FAILURE, state.model_dump_json(indent=2)
    assert state.status is RunStatus.STOPPED
    assert len(state.attempts) == 2
    assert state.attempts[0].patch_hash != state.attempts[1].patch_hash
    assert state.attempts[0].failure_signatures == state.attempts[1].failure_signatures

    visited = [item["node"] for item in state.events]
    assert visited.count(Node.RUN_TARGET_TESTS.value) == 2
    assert visited.count(Node.CHECK_BUDGET.value) == 2
    # 停止之后没有任何新的外部副作用：最后一次 check_budget 之后不再修补丁。
    assert visited[-1] == Node.FINALIZE.value
    last_check = len(visited) - 1 - visited[::-1].index(Node.CHECK_BUDGET.value)
    assert Node.REPAIR_PATCH.value not in visited[last_check + 1 :]
    assert harness.deps.test_runs == 4  # 基线 2 + 两轮目标测试


def test_repeated_failure_explanation_names_both_rounds(tmp_path: Path) -> None:
    """停止原因不只一个命中时，报告要能看出真实缺口，而不是只留一个标签。"""
    harness = build_harness(
        tmp_path,
        results=[_failure(NODE, "assert 1 == 3"), _failure(NODE, "assert 1 == 3")],
        coder_rounds=[
            [_edit("    return value - 1", "    return value - 2"), FINISH],
            [_edit("    return value - 1", "    return value - 3"), FINISH],
        ],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
        extra_plans=1,
    )
    state = harness.run()
    reasons = " ".join(state.failure_reasons)
    assert "repeated_failure rounds=1,2" in reasons


def test_same_signature_different_failing_test_is_not_treated_as_repeated(
    tmp_path: Path,
) -> None:
    """DA-010 负控制：签名相同但失败的是**另一个测试**，不能当同一根因停下来。"""
    harness = build_harness(
        tmp_path,
        results=[
            _failure("tests/test_core.py::test_first", "assert 1 == 3"),
            _failure("tests/test_core.py::test_second", "assert 1 == 3"),
            passed_result("tests/test_core.py::test_third"),
        ],
        coder_rounds=[
            [_edit("    return value - 1", "    return value - 2"), FINISH],
            [_edit("    return value - 1", "    return value - 3"), FINISH],
            [_edit("    return value - 3", "    return value"), FINISH],
        ],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
        budget=RepairBudget(max_iterations=5, max_test_runs=10),
        extra_plans=1,
    )
    state = harness.run()

    assert state.stop_reason is not StopReason.REPEATED_FAILURE, state.model_dump_json(indent=2)
    # 系统没有因为误聚合提前停下，而是继续修到通过。
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert len(state.attempts) == 3
