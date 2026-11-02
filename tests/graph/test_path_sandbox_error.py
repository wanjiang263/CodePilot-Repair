"""G-21：``ENV_ERROR`` / ``SANDBOX_ERROR`` 直接终止，绝不进入修复循环。

03 §14 与 02 §13.4 把环境问题排除在诊断循环之外，理由是容器坏了不是代码坏了：
让它进 ``diagnose`` 会得到一个把环境问题"归因给补丁"的假诊断，然后一轮轮修补丁
去修一个根本不在补丁里的错。

OOM 单独列一条：Phase 4 已经把 ``State.OOMKilled`` 映射成
``SANDBOX_ERROR / OOM_KILLED``，所以它必须**天然**走同一条终止边 —— 图里不该
再有第二个 ``error_reason == "OOM_KILLED"`` 的判断分支。

COLLECTION_ERROR / TIMEOUT 是对照组：它们**确实**进诊断路径。
"""

from __future__ import annotations

from pathlib import Path

from harness import (
    FINISH,
    assertion_failure,
    build_harness,
    diagnosis,
    scripted_result,
)

from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason


def _edit() -> dict[str, object]:
    return {
        "tool": "edit_file",
        "arguments": {
            "path": "src/pkg/core.py",
            "search": "    return value - 1",
            "replace": "    return value - 2",
        },
    }


def _env_error() -> object:
    return scripted_result(ExecutionStatus.ENV_ERROR, exit_code=None)


def _sandbox_error(reason: str, *, oom: bool = False) -> object:
    result = scripted_result(ExecutionStatus.SANDBOX_ERROR, exit_code=137 if oom else None)
    return result.model_copy(
        update={
            "error_reason": reason,
            "diagnostics": {
                **result.diagnostics,
                **({"oom_killed": True} if oom else {}),
            },
        }
    )


def _harness(tmp_path: Path, broken: object):
    return build_harness(
        tmp_path,
        results=[broken],
        coder_rounds=[[_edit(), FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )


def _assert_terminal_without_diagnosis(tmp_path: Path, broken: object, reason: str) -> object:
    harness = _harness(tmp_path, broken)
    state = harness.run()

    assert state.stop_reason is StopReason.SANDBOX_FAILURE, state.model_dump_json()[:400]
    assert state.status is RunStatus.SANDBOX_ERROR
    visited = [item["node"] for item in state.events]
    # 终止边：没有诊断、没有再修补丁。
    assert Node.DIAGNOSE.value not in visited, visited
    assert Node.REPAIR_PATCH.value not in visited, visited
    assert Node.CHECK_BUDGET.value not in visited, visited
    assert Node.FINALIZE.value in visited
    # 环境错误仍然如实上报原因。
    assert any(reason in item for item in state.failure_reasons)
    # 已经真实执行过的测试仍然计费，不能因为"是环境问题"就不算。
    assert harness.deps.test_runs == 3
    return state


def test_environment_error_finalizes_directly(tmp_path: Path) -> None:
    _assert_terminal_without_diagnosis(tmp_path, _env_error(), "ENV_ERROR")


def test_sandbox_error_finalizes_directly(tmp_path: Path) -> None:
    _assert_terminal_without_diagnosis(
        tmp_path, _sandbox_error("DOCKER_OR_CONTAINER_ERROR"), "SANDBOX_ERROR"
    )


def test_oom_follows_the_sandbox_branch_without_special_handling(tmp_path: Path) -> None:
    """OOM 沿用 Phase 4 的 ``SANDBOX_ERROR/OOM_KILLED``，不需要新的状态或分支。"""
    broken = _sandbox_error("OOM_KILLED", oom=True)
    state = _assert_terminal_without_diagnosis(tmp_path, broken, "OOM_KILLED")
    assert state.execution is not None
    assert state.execution.diagnostics["oom_killed"] is True
    assert state.execution.status is ExecutionStatus.SANDBOX_ERROR


def test_environment_failure_during_build_stops_before_any_code_work(
    tmp_path: Path,
) -> None:
    """环境镜像建不出来时，不再往下做任何会产生费用的事。

    节点顺序（02 §15.2）是 analyze_impact **之后**才 build_env，所以 Issue 分析
    与定位已经发生 —— 这里断言的是规划与编码没有发生，而不是整条链都没跑。
    """
    harness = build_harness(
        tmp_path,
        results=[assertion_failure()],
        coder_rounds=[[_edit(), FINISH]],
        build_status=ExecutionStatus.ENV_ERROR,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.SANDBOX_FAILURE
    assert state.status is RunStatus.SANDBOX_ERROR
    visited = [item["node"] for item in state.events]
    for node in (
        Node.PLAN,
        Node.GENERATE_PATCH,
        Node.VALIDATE_PATCH,
        Node.RUN_TARGET_TESTS,
    ):
        assert node.value not in visited, visited
    purposes = harness.llm.purposes()
    assert "planner" not in purposes
    assert "coder" not in purposes
    assert harness.deps.test_runs == 0
    assert state.plan is None
    assert state.current_patch == ""


def test_collection_error_still_enters_the_diagnosis_path(tmp_path: Path) -> None:
    """对照组：COLLECTION_ERROR 是代码问题（补丁让测试收集不起来了），要进诊断。"""
    collection = scripted_result(ExecutionStatus.COLLECTION_ERROR, exit_code=2).model_copy(
        update={"stderr_tail": "src/pkg/core.py:2: SyntaxError: invalid patched expression"}
    )
    harness = build_harness(
        tmp_path,
        results=[collection, assertion_failure("tests/test_core.py::test_normalize")],
        coder_rounds=[
            [_edit(), FINISH],
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 2",
                        "replace": "    return value",
                    },
                },
                FINISH,
            ],
        ],
        diagnoses=[
            diagnosis(
                "refine",
                file="tests/test_core.py",
                line=4,
                text="assert normalize(3) == 3",
            ),
            diagnosis("give_up", line=2, text="return value"),
        ],
    )
    state = harness.run()

    visited = [item["node"] for item in state.events]
    assert Node.DIAGNOSE.value in visited
    assert Node.REPAIR_PATCH.value in visited
    # 第 2 轮修好后不再诊断，被 give_up 之外的路径收尾。
    assert state.attempts[0].execution is not None
    assert state.attempts[0].execution.status is ExecutionStatus.COLLECTION_ERROR


def test_timeout_still_enters_the_diagnosis_path(tmp_path: Path) -> None:
    """TIMEOUT 同样是"这次运行没通过"，按 02 的边表进诊断。"""
    timeout = scripted_result(ExecutionStatus.TIMEOUT, exit_code=None)
    timeout = timeout.model_copy(update={"error_reason": "EXECUTION_TIMEOUT"})
    harness = build_harness(
        tmp_path,
        results=[timeout],
        coder_rounds=[[_edit(), FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        budget=RepairBudget(max_iterations=0),
    )
    state = harness.run()

    visited = [item["node"] for item in state.events]
    assert Node.DIAGNOSE.value in visited
    assert state.stop_reason is StopReason.BUDGET_ITERATIONS
