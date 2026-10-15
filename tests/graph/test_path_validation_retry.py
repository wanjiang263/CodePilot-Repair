"""G-22：Patch 校验失败 → 回 check_budget → 重新生成。

四件事必须同时成立，缺一条就是"看起来能重试，实际上在烧预算"：

* 无效补丁**从未送去执行**：Sandbox 调用次数里不含它；
* 每一轮无效补丁都留下记录（否则 ``validation_loop`` 数不出来，也没法回答
  "模型反复交了什么"）；
* 连续三次无效触发 ``validation_loop`` 停止；
* 修好之后的有效补丁才可以执行 Sandbox，并且之前的失败记录不被清除。
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness, passed_result

from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason

VALID = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value",
    },
}


def _protected(relative: str, content: str) -> dict[str, object]:
    """新建一个受保护配置文件 —— 路径检查一定拒绝，且每轮补丁都不同。"""
    return {"tool": "create_file", "arguments": {"path": relative, "content": content}}


INVALID_A = _protected("pytest.ini", "[pytest]\naddopts = -x\n")
INVALID_B = _protected("tox.ini", "[tox]\nenvlist = py312\n")
INVALID_C = _protected("setup.cfg", "[metadata]\nname = fixture\n")


def test_invalid_patch_is_never_executed_then_repaired(tmp_path: Path) -> None:
    """第 1 轮模型直接 finish、什么也没改 → 空补丁被拒 → 第 2 轮才真正修。

    这里刻意不用"写坏受保护文件"当无效样例：那种补丁已经**落在 Workspace 里**，
    refine 只会往上叠加，第二轮再怎么改也仍然带着那个受保护文件。真实恢复路径是
    连续无效直到 ``validation_loop``（下面的用例覆盖），而不是"立刻变有效"。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result("tests/test_core.py::test_normalize")],
        coder_rounds=[[FINISH], [VALID, FINISH]],
    )
    state = harness.run()

    assert state.stop_reason is None, state.status.value
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED
    assert len(state.attempts) == 2
    first, second = state.attempts
    assert not first.validation.passed
    assert first.execution is None, "an invalid patch must never reach the sandbox"
    assert second.validation.passed
    assert second.execution is not None and second.execution.status.value == "PASS"

    # Sandbox 只被调用了基线 2 次 + 有效补丁 1 次；无效补丁那次不算。
    assert harness.deps.test_runs == 3
    assert state.test_runs == 3

    visited = [item["node"] for item in state.events]
    assert visited.count(Node.VALIDATE_PATCH.value) == 2
    assert visited.count(Node.CHECK_BUDGET.value) == 1
    assert visited.count(Node.REPAIR_PATCH.value) == 1
    # 第 2 轮才第一次执行目标测试。
    assert visited.index(Node.RUN_TARGET_TESTS.value) > visited.index(Node.REPAIR_PATCH.value)


def test_three_consecutive_invalid_patches_stop_the_run(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result("tests/test_core.py::test_normalize")],
        coder_rounds=[
            [INVALID_A, FINISH],
            [INVALID_B, FINISH],
            [INVALID_C, FINISH],
        ],
        budget=RepairBudget(max_iterations=5),
    )
    state = harness.run()

    assert state.stop_reason is StopReason.VALIDATION_LOOP, state.status.value
    assert state.status is RunStatus.STOPPED
    assert len(state.attempts) == 3
    assert all(not item.validation.passed for item in state.attempts)
    # 三轮无效补丁一次都没执行过测试：只有基线那两次。
    assert harness.deps.test_runs == 2
    assert "consecutive patch validation failures=3" in " ".join(state.failure_reasons)
    visited = [item["node"] for item in state.events]
    assert Node.RUN_TARGET_TESTS.value not in visited


def test_rejected_patches_are_kept_with_their_reasons(tmp_path: Path) -> None:
    """失败轮次的原因必须留在历史里，否则无法回答"模型反复交了什么"。"""
    harness = build_harness(
        tmp_path,
        results=[],
        coder_rounds=[[INVALID_A, FINISH], [INVALID_B, FINISH], [INVALID_C, FINISH]],
        budget=RepairBudget(max_iterations=5),
    )
    state = harness.run()

    assert state.stop_reason is StopReason.VALIDATION_LOOP
    for item in state.attempts:
        assert item.validation.errors
        assert item.patch_hash
        assert item.patch_diff  # 被拒的补丁同样要能复盘
    assert len({item.patch_hash for item in state.attempts}) == 3
