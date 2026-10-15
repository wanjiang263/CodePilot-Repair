"""G-16：第 1 轮失败、第 2 轮通过。

这条路径是"多轮自主修复"和"给 Phase 5 套一层 for 循环"的分水岭，所以断言都盯在
**真实副作用**上，而不是返回值：

* 两轮补丁都必须真的落到隔离 Workspace 上，并且互不相同；
* 第 1 轮的失败必须经 Phase 7 的解析器变成结构化 ``ParsedFailure``，再经签名、
  压缩进入 Diagnosis，而不是凭空造一个失败字符串；
* 第 2 轮的 Coding Agent 必须是在**第 1 轮改坏的文件**上继续编辑（search 块只有
  在第 1 轮真的落盘后才存在），这是"反馈真的进入了下一轮"的可证伪证据；
* Attempt 历史两轮都在，第 1 轮不被覆盖。

``FakeSandbox`` 在这里只负责"跑测试"这一件事；补丁内容由 Coding Agent 真实写出，
测试不是因为脚本写着 PASS 才 PASS。
"""

from __future__ import annotations

from pathlib import Path

from harness import (
    CORRECT,
    assert_original_clean,
    assertion_failure,
    build_harness,
    diagnosis,
    passed_result,
)

from codepilot.graph.routing import Node
from codepilot.patch.diff import generate_git_diff
from codepilot.schemas.enums import RepairStrategy, RunStatus

ROUND1 = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value - 2",
    },
}
# 这一段的 search 只有在第 1 轮真的写进了 Workspace 之后才可能存在。
ROUND2 = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 2",
        "replace": "    return value",
    },
}
FINISH = {"tool": "finish", "arguments": {"summary": "done"}}


def _two_round_harness(tmp_path: Path):
    return build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            passed_result("tests/test_core.py::test_normalize"),
        ],
        coder_rounds=[[ROUND1, FINISH], [ROUND2, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )


def test_second_round_repairs_after_real_failure(tmp_path: Path) -> None:
    harness = _two_round_harness(tmp_path)
    state = harness.run()

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert len(state.attempts) == 2, [
        a.model_dump(include={"iteration", "patch_hash"}) for a in state.attempts
    ]

    first, second = state.attempts
    assert first.iteration == 1 and second.iteration == 2
    assert first.patch_hash != second.patch_hash
    # 第 1 轮的失败证据没有被第 2 轮覆盖。
    assert first.execution is not None and first.execution.status.value == "FAIL"
    assert second.execution is not None and second.execution.status.value == "PASS"
    assert first.failure_signatures and second.failure_signatures == []

    # 两轮补丁都真的落在 Workspace 上，最终文件是被第 2 轮修好的。
    assert harness.source() == CORRECT
    assert_first_patch_changed_the_wrong_way(first.patch_diff)
    assert_original_clean(harness)


def assert_first_patch_changed_the_wrong_way(diff: str) -> None:
    """第 1 轮确实把代码改成了另一个错误值，而不是没动。"""
    assert "-    return value - 1" in diff
    assert "+    return value - 2" in diff


def test_round_two_actually_edited_the_previous_round_output(tmp_path: Path) -> None:
    """反馈真的进入了下一轮：第 2 轮的编辑块只能在第 1 轮落盘后存在。

    如果 repair_patch 没有在第 1 轮的 Workspace 上继续（比如偷偷回退了或重建了
    文件），``ROUND2`` 的 search 块就匹配不到，edit_file 会失败、补丁不会变化、
    目标测试也不会过——这条断言因此不是摆设。
    """
    harness = _two_round_harness(tmp_path)
    state = harness.run()

    assert harness.sandbox.run_calls, "sandbox was never invoked"
    # ``tool_calls`` 是**每轮**的工具记录（预算口径就是"本轮调了几次工具"），
    # 所以这里断言的是最后一轮那 2 次，而不是两轮合计的 4 次。
    assert [item["name"] for item in state.tool_calls] == ["edit_file", "finish"]
    assert all(item["ok"] for item in state.tool_calls)
    # 两轮各编辑一次：第 1 轮的编辑痕迹只能靠补丁内容证明。
    assert state.attempts[0].patch_diff != state.attempts[1].patch_diff
    assert "-    return value - 1" in state.attempts[0].patch_diff
    assert "-    return value - 1" in state.attempts[1].patch_diff
    assert "+    return value - 2" in state.attempts[0].patch_diff
    assert "+    return value - 2" not in state.attempts[1].patch_diff

    patch = generate_git_diff(harness.workspace, state.base_commit)
    assert patch.files == ["src/pkg/core.py"]


def test_failure_flows_through_the_phase7_pipeline(tmp_path: Path) -> None:
    """失败必须经过解析→分类→签名→压缩→诊断，而不是被拼成一句字符串。"""
    harness = _two_round_harness(tmp_path)
    state = harness.run()

    first = state.attempts[0]
    assert first.execution is not None
    failed = first.execution.failed_tests
    assert failed and failed[0].failure_type == "AssertionError"
    assert len(first.failure_signatures) == 1

    # 诊断产物是真实模型输出经 evidence 校验后的结果。
    assert first.diagnosis is not None
    assert first.diagnosis.repair_strategy is RepairStrategy.REFINE
    assert first.diagnosis.evidence, "diagnosis kept no verified evidence"
    assert first.diagnosis.evidence[0].file == "src/pkg/core.py"

    # 诊断确实被调用过，且没有把 gold patch 塞进 prompt。
    purposes = harness.llm.purposes()
    assert purposes.count("diagnoser") == 1
    assert all("return value\n" not in text for text in harness.llm.prompt_texts()[-2:])

    visited = [item["node"] for item in state.events]
    assert visited.count(Node.DIAGNOSE.value) == 1
    assert visited.count(Node.REPAIR_PATCH.value) == 1
    assert visited.count(Node.CHECK_BUDGET.value) == 1


def test_progress_snapshot_records_target_failure_then_recovery(tmp_path: Path) -> None:
    harness = _two_round_harness(tmp_path)
    state = harness.run()

    first, second = state.attempts
    assert first.progress is not None and first.progress.target_failed == 1
    assert second.progress is not None and second.progress.target_failed == 0
    # 回归本轮未测量，必须显式说出来而不是写成 0。
    assert first.progress.regression_measured is False

    # 预算只按真实执行计费：基线 2 次 + 两轮目标测试各 1 次。
    assert state.test_runs == 4
    assert state.usage.llm_calls == len(harness.llm.calls) > 0
