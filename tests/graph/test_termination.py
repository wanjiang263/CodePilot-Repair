"""G-24：任何路径都必须在有限步数内结束。

每条场景都要跑到一个**终态**，并且断言实际用的步数严格小于本次预算推导出的
``recursion_limit``。后一条才是真正的验收点：如果只有上限在救场，说明停止规则
没工作，图其实是在自旋。

覆盖 04 Phase 8 验收列出的全部路径形状，另加三条容易被漏掉的畸形输入：
模型产出无法解析的结构化输出、一个目标测试都没有、以及复现降级。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from harness import (
    FINISH,
    Harness,
    assertion_failure,
    build_harness,
    diagnosis,
    multiple_failures,
    passed_result,
)

from codepilot.graph.builder import recursion_limit_for, supersteps_used
from codepilot.graph.checkpoint import open_checkpointer, resume_run, run_with_checkpoint
from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason
from codepilot.schemas.state import RepairState

TERMINAL = {
    RunStatus.PROVISIONALLY_VERIFIED,
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.STOPPED,
    RunStatus.ERROR,
    RunStatus.SANDBOX_ERROR,
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.PATCH_REJECTED,
    RunStatus.TESTS_FAILED,
    RunStatus.TOOL_ERROR,
}

EDIT = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value - 2",
    },
}
EDIT_AGAIN = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 2",
        "replace": "    return value - 3",
    },
}
#: revert_and_retry 会把 Workspace 恢复到 base，所以第 2 轮必须从 ``- 1`` 出发。
EDIT_AFTER_REVERT = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value - 3",
    },
}
PROTECTED = {"tool": "create_file", "arguments": {"path": "pytest.ini", "content": "[pytest]\n"}}
NODE = "tests/test_core.py::test_normalize"

BUDGET = RepairBudget(max_iterations=5, max_test_runs=12)


# ------------------------------------------------------------------ 场景库


def _first_pass(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[passed_result(NODE)],
        coder_rounds=[[EDIT, FINISH]],
    )


def _multi_round_failure(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[
            multiple_failures(1, prefix="alpha"),
            multiple_failures(2, prefix="bravo"),
            multiple_failures(3, prefix="charlie"),
        ],
        coder_rounds=[[EDIT, FINISH], [EDIT_AGAIN, FINISH], [EDIT, FINISH]],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
            diagnosis("refine", line=2, text="return value - 2"),
        ],
        budget=BUDGET,
    )


def _repeated_patch(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[assertion_failure(NODE), assertion_failure(NODE)],
        coder_rounds=[[EDIT, FINISH], [EDIT, FINISH]],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 2"),
        ],
        extra_plans=1,
        budget=BUDGET,
    )


def _repeated_failure(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[
            assertion_failure(NODE, message="assert 1 == 3"),
            assertion_failure(NODE, message="assert 0 == 3"),
        ],
        coder_rounds=[[EDIT, FINISH], [EDIT_AFTER_REVERT, FINISH]],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
        extra_plans=1,
        budget=BUDGET,
    )


def _oscillation(tmp_path: Path) -> Harness:
    """A → B → A，且三轮补丁互不相同。

    第 3 轮如果交出与第 1 轮逐字相同的补丁，先命中的会是 ``repeated_patch``
    （优先级更高），测不到振荡判据。这里让第 3 轮改成 ``+ 5``：结果仍是正数，
    消息归一化后与第 1 轮同为 ``assert <number> == <number>``，签名相同而补丁不同。
    """
    return build_harness(
        tmp_path,
        results=[
            assertion_failure(NODE, message="assert 1 == 3"),
            scripted_type_failure(),
            assertion_failure(NODE, message="assert 8 == 3"),
        ],
        coder_rounds=[
            [EDIT, FINISH],
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 2",
                        "replace": '    return value + "x"',
                    },
                },
                FINISH,
            ],
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": '    return value + "x"',
                        "replace": "    return value + 5",
                    },
                },
                FINISH,
            ],
        ],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text='return value + "x"'),
            diagnosis("refine", line=2, text="return value + 5"),
        ],
        budget=BUDGET,
    )


def scripted_type_failure() -> object:
    from harness import scripted_result

    from codepilot.schemas.enums import TestOutcome
    from codepilot.schemas.execution import TestCaseResult

    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=NODE,
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type="TypeError",
                message="cannot add int and str",
                traceback="src/pkg/core.py:2: TypeError: cannot add int and str",
            )
        ],
        exit_code=1,
    )


def _budget_iterations(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[assertion_failure(NODE)],
        coder_rounds=[[EDIT, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        budget=RepairBudget(max_iterations=0),
    )


def _budget_test_runs(tmp_path: Path) -> Harness:
    return build_harness(
        tmp_path,
        results=[assertion_failure(NODE)],
        coder_rounds=[[EDIT, FINISH]],
        budget=RepairBudget(max_test_runs=2),
    )


def _validation_loop(tmp_path: Path) -> Harness:
    """三次**互不相同**的无效补丁。

    用同一份无效补丁会先命中 ``repeated_patch``（优先级在 ``validation_loop`` 之后，
    但第 2 轮的重复就已经能判定），要测的是校验循环本身，所以三份补丁必须不同。
    """
    return build_harness(
        tmp_path,
        results=[],
        coder_rounds=[
            [
                {
                    "tool": "create_file",
                    "arguments": {"path": "pytest.ini", "content": "[pytest]\n"},
                },
                FINISH,
            ],
            [
                {"tool": "create_file", "arguments": {"path": "tox.ini", "content": "[tox]\n"}},
                FINISH,
            ],
            [
                {"tool": "create_file", "arguments": {"path": "setup.cfg", "content": "[x]\n"}},
                FINISH,
            ],
        ],
        budget=BUDGET,
    )


def _structured_output_error(tmp_path: Path) -> Harness:
    """模型交出的规划 JSON 永远不合法。"""
    return build_harness(
        tmp_path,
        results=[assertion_failure(NODE)],
        coder_rounds=[[EDIT, FINISH]],
        planner_replies=[{"totally_not_a_repair_plan": True}],
    )


def _sandbox_error(tmp_path: Path) -> Harness:
    broken = assertion_failure(NODE).model_copy(
        update={"status": ExecutionStatus.SANDBOX_ERROR, "error_reason": "DOCKER_ERROR"}
    )
    return build_harness(
        tmp_path,
        results=[broken],
        coder_rounds=[[EDIT, FINISH]],
    )


def _no_target_tests(tmp_path: Path) -> Harness:
    """一个目标测试都没有：不能伪造"目标测试通过"。"""
    from harness import _planner_reply  # 走同一份规划脚本，只是把测试清单清空

    del _planner_reply
    return build_harness(
        tmp_path,
        results=[passed_result(NODE)],
        coder_rounds=[[EDIT, FINISH]],
        tests=(),
    )


def _reproduction_degraded(tmp_path: Path) -> Harness:
    """没有复现能力时如实记录 SKIPPED，并降级到 Planner 选出的已有测试。"""
    return build_harness(
        tmp_path,
        results=[passed_result(NODE)],
        coder_rounds=[[EDIT, FINISH]],
    )


SCENARIOS: dict[str, Callable[[Path], Harness]] = {
    "first_pass": _first_pass,
    "multi_round_failure": _multi_round_failure,
    "repeated_patch": _repeated_patch,
    "repeated_failure": _repeated_failure,
    "no_progress_oscillation": _oscillation,
    "budget_iterations": _budget_iterations,
    "budget_test_runs": _budget_test_runs,
    "validation_loop": _validation_loop,
    "structured_output_error": _structured_output_error,
    "sandbox_error": _sandbox_error,
    "no_target_tests": _no_target_tests,
    "reproduction_degraded": _reproduction_degraded,
}


# ------------------------------------------------------------------ 主断言


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_path_terminates_inside_the_recursion_limit(tmp_path: Path, name: str) -> None:
    harness = SCENARIOS[name](tmp_path)
    state = harness.run()

    assert state.status in TERMINAL, (name, state.status)
    assert state.status is not RunStatus.RUNNING
    assert [item["node"] for item in state.events][-1] == Node.FINALIZE.value

    # 真正生效的是 LangGraph 的上限：步数必须严格小于它，否则是在自旋。
    limit = recursion_limit_for(state.budget)
    steps = supersteps_used(state)
    assert steps < limit, (name, steps, limit)
    # 每一轮 Attempt 都完整，没有半成品轮次。
    assert all(item.validation is not None for item in state.attempts)
    assert [item.iteration for item in state.attempts] == list(range(1, len(state.attempts) + 1))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_scenario_leaves_the_original_repository_dirty(tmp_path: Path, name: str) -> None:
    from codepilot.repo.gitcmd import run_git

    harness = SCENARIOS[name](tmp_path)
    harness.run()
    assert run_git(["status", "--porcelain"], cwd=harness.original).stdout == ""


def test_stop_reasons_are_all_reachable_and_distinct(tmp_path: Path) -> None:
    """不同场景必须落到不同原因上，否则"覆盖了全部停止条件"是假的。"""
    seen: dict[str, StopReason] = {}
    for name in (
        "repeated_patch",
        "repeated_failure",
        "no_progress_oscillation",
        "budget_iterations",
        "budget_test_runs",
        "validation_loop",
        "sandbox_error",
        "structured_output_error",
    ):
        probe = tmp_path / name
        probe.mkdir()
        state = SCENARIOS[name](probe).run()
        assert state.stop_reason is not None, name
        seen[name] = state.stop_reason

    assert seen["repeated_patch"] is StopReason.REPEATED_PATCH
    assert seen["repeated_failure"] is StopReason.REPEATED_FAILURE
    assert seen["no_progress_oscillation"] is StopReason.NO_PROGRESS
    assert seen["budget_iterations"] is StopReason.BUDGET_ITERATIONS
    assert seen["budget_test_runs"] is StopReason.BUDGET_TEST_RUNS
    assert seen["validation_loop"] is StopReason.VALIDATION_LOOP
    assert seen["sandbox_error"] is StopReason.SANDBOX_FAILURE
    assert seen["structured_output_error"] is StopReason.WORKFLOW_ERROR
    assert len(set(seen.values())) == len(seen), seen


def test_no_target_tests_never_claims_a_passing_target(tmp_path: Path) -> None:
    probe = tmp_path / "no-targets"
    probe.mkdir()
    state = _no_target_tests(probe).run()
    assert state.status is RunStatus.FAILED, state.status
    assert any("never executed" in item for item in state.failure_reasons)


def test_reproduction_degradation_still_reaches_the_target_tests(tmp_path: Path) -> None:
    harness = _reproduction_degraded(tmp_path)
    state = harness.run()
    assert state.reproduction is not None
    assert state.reproduction.status.value == "skipped"
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED
    # 降级后用的确实是 Planner 选出的已有测试。
    assert state.plan is not None and state.plan.tests_to_run


def test_recovery_from_a_terminal_checkpoint_terminates_immediately(tmp_path: Path) -> None:
    """已完成的 run 再"恢复"一次也必须立刻结束，不能重跑外部副作用。"""
    probe = tmp_path / "resume-terminal"
    probe.mkdir()
    harness = _first_pass(probe)
    checkpoint = probe / "artifacts" / "runs" / "phase8-run" / "checkpoints.sqlite"

    async def go() -> tuple[RepairState, RepairState]:
        async with await open_checkpointer(checkpoint, "phase8-run") as handle:
            first = await run_with_checkpoint(harness.initial, harness.deps, handle)
            second = await resume_run(harness.deps, handle, "phase8-run")
        return first, second

    first, second = asyncio.run(go())
    assert first.status is RunStatus.PROVISIONALLY_VERIFIED
    assert second.status is first.status
    assert second.attempts == first.attempts
    assert harness.deps.test_runs == 3
