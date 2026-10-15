"""G-18：同一份补丁被重复产生 → ``repeated_patch``。

最真实的触发路径是 ``revert_and_retry``：Workspace 回到 base 之后，模型很容易
交出与第 1 轮**逐字相同**的补丁。这时继续跑没有任何意义，必须停。

另一半是边界：只差格式（缩进）与语义不同的补丁**不能**被判成重复。哈希规范化的
取舍在 :mod:`codepilot.graph.stop_rules` 的单元测试里逐条钉住，这里验的是图真的
按那些哈希做决定，以及历史哈希可追溯。
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness, diagnosis, scripted_result

from codepilot.graph.routing import Node
from codepilot.graph.stop_rules import patch_hash
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult

NODE = "tests/test_core.py::test_normalize"


def _failure(message: str) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=NODE,
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


SAME_TWICE = _edit("    return value - 1", "    return value - 2")


def test_identical_patch_after_revert_stops_the_loop(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        # 两轮失败内容完全一致，但先命中的是 repeated_patch（优先级更高）。
        results=[_failure("assert 1 == 3"), _failure("assert 1 == 3")],
        coder_rounds=[[SAME_TWICE, FINISH], [SAME_TWICE, FINISH]],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 2"),
        ],
        extra_plans=1,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.REPEATED_PATCH, state.status.value
    assert state.status is RunStatus.STOPPED
    assert len(state.attempts) == 2
    first, second = state.attempts
    # 历史里的补丁哈希可追溯：两轮哈希一致，且都等于对补丁文本重算出来的值。
    assert first.patch_hash == second.patch_hash
    assert first.patch_hash == patch_hash(first.patch_diff)
    assert "patch identical to iteration 1" in " ".join(state.failure_reasons)
    # 停在这里，不再有第三次 Sandbox 执行。
    assert harness.deps.test_runs == 4
    visited = [item["node"] for item in state.events]
    assert visited.count(Node.RUN_TARGET_TESTS.value) == 2


def test_different_patch_is_not_reported_as_repeated(tmp_path: Path) -> None:
    """语义不同的第二轮补丁不能命中 repeated_patch。"""
    harness = build_harness(
        tmp_path,
        results=[_failure("assert 1 == 3"), _failure("assert 1 == 3")],
        coder_rounds=[
            [SAME_TWICE, FINISH],
            [_edit("    return value - 1", "    return value - 3"), FINISH],
        ],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
        extra_plans=1,
    )
    state = harness.run()

    assert state.stop_reason is not StopReason.REPEATED_PATCH
    assert state.attempts[0].patch_hash != state.attempts[1].patch_hash
    # 换掉的仍是同一批失败，所以停下来的原因是 repeated_failure。
    assert state.stop_reason is StopReason.REPEATED_FAILURE


def test_formatting_only_difference_is_a_different_patch(tmp_path: Path) -> None:
    """按我们的口径，**行尾/缩进差异不被抹掉**，所以它不是同一份补丁。

    这条断言把 ``canonical_patch`` 的取舍钉在图级别：有人若把"去掉所有空白"
    当作简化改回去，这里会先红。
    """
    reindented = _edit("    return value - 1", "        return value - 2")
    assert patch_hash("a\n+b\n") != patch_hash("a\n+  b\n")

    harness = build_harness(
        tmp_path,
        results=[_failure("assert 1 == 3"), _failure("assert 1 == 3")],
        coder_rounds=[
            [SAME_TWICE, FINISH],
            [reindented, FINISH],
        ],
        diagnoses=[
            diagnosis("revert_and_retry", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 2"),
        ],
        extra_plans=1,
    )
    state = harness.run()
    # 缩进改变语义，因此不是重复补丁；第 2 轮的编辑甚至可能直接失败。
    assert state.attempts[0].patch_hash != state.attempts[1].patch_hash
