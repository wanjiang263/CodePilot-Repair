"""G-19：连续无进展 → ``no_progress``。

两种形状都要覆盖，因为它们是**不同的失效模式**：

* 失败越修越多（1 → 2 → 3）：签名每轮都变，所以 ``repeated_failure`` 不会命中，
  只有进度判定能拦住它；
* 振荡 A → B → A：每轮"看起来都在变"，单看 ``made_progress`` 甚至是有进展，
  必须回看**更早**的轮次才看得出来在原地打转（02 §15.4 末段）。

前一种在停止优先级里排在 ``repeated_failure`` 之后，所以两种形状分别断言
primary 与 ``all_reasons``，不把其中一个的结果冒充成另一个。
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness, diagnosis, multiple_failures, scripted_result

from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult

BUDGET = RepairBudget(max_iterations=5, max_test_runs=12)


def _edit(search: str, replace: str) -> dict[str, object]:
    return {
        "tool": "edit_file",
        "arguments": {"path": "src/pkg/core.py", "search": search, "replace": replace},
    }


def _named_failure(node_id: str, *, exception: str, message: str) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=node_id,
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type=exception,
                message=message,
                traceback=f"src/pkg/core.py:2: {exception}: {message}",
            )
        ],
        exit_code=1,
    )


NODE = "tests/test_core.py::test_normalize"


def test_worsening_failures_stop_as_no_progress(tmp_path: Path) -> None:
    """失败数一轮比一轮多：没有重复补丁也没有重复失败，只有进度判定能拦。"""
    harness = build_harness(
        tmp_path,
        results=[
            multiple_failures(1, prefix="alpha"),
            multiple_failures(2, prefix="bravo"),
            multiple_failures(3, prefix="charlie"),
        ],
        coder_rounds=[
            [_edit("    return value - 1", "    return value - 2"), FINISH],
            [_edit("    return value - 2", "    return value - 3"), FINISH],
            [_edit("    return value - 3", "    return value - 4"), FINISH],
        ],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
            diagnosis("refine", line=2, text="return value - 4"),
        ],
        budget=BUDGET,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.NO_PROGRESS, state.status.value
    assert state.status is RunStatus.STOPPED
    assert len(state.attempts) == 3
    # 三轮的签名集合互不相同，所以这不是 repeated_failure。
    sets = [set(item.failure_signatures) for item in state.attempts]
    assert sets[0] != sets[1] != sets[2]
    assert StopReason.REPEATED_FAILURE not in state.model_dump_json()
    reasons = " ".join(state.failure_reasons)
    assert "no_progress_streak=2" in reasons


def test_oscillation_back_to_an_earlier_signature_set_stops(tmp_path: Path) -> None:
    """A → B → A：第 3 轮的失败集合在第 1 轮就出现过，判为无进展。

    第 3 轮的补丁必须与第 1 轮**不同**，否则 ``repeated_patch``（优先级更高）会
    先命中，测到的就不是振荡判据。这里用 ``- 7`` 换回同一批断言失败：两条消息
    归一化后都是 ``assert <number> == <number>``，签名一致而补丁不同。
    """
    harness = build_harness(
        tmp_path,
        results=[
            _named_failure(NODE, exception="AssertionError", message="assert 1 == 3"),
            _named_failure(NODE, exception="TypeError", message="cannot add int and str"),
            _named_failure(NODE, exception="AssertionError", message="assert 2 == 3"),
        ],
        coder_rounds=[
            [_edit("    return value - 1", "    return value - 2"), FINISH],
            [_edit("    return value - 2", '    return value + "x"'), FINISH],
            [_edit('    return value + "x"', "    return value - 7"), FINISH],
        ],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text='return value + "x"'),
            diagnosis("refine", line=2, text="return value - 7"),
        ],
        budget=BUDGET,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.NO_PROGRESS, state.status.value
    assert len(state.attempts) == 3
    first, middle, last = state.attempts
    assert set(first.failure_signatures) == set(last.failure_signatures)
    assert first.failure_signatures != middle.failure_signatures
    # 三轮补丁互不相同，所以停下来的原因不可能是重复补丁。
    assert len({item.patch_hash for item in state.attempts}) == 3
    reasons = " ".join(state.failure_reasons)
    assert "oscillation back to round 1" in reasons
    visited = [item["node"] for item in state.events]
    # 每轮 diagnose 之后都过一次 check_budget；第 3 次才判停。
    assert visited.count(Node.CHECK_BUDGET.value) == 3
    assert visited.count(Node.REPAIR_PATCH.value) == 2
    assert visited[-1] == Node.FINALIZE.value
