"""G-15：一次通过 —— 首个补丁通过校验、目标测试 PASS。

这条路径同时是整套状态机的冒烟测试：它真的在跑 LangGraph 的 StateGraph（断言
事件轨迹里出现了预期的节点序列），而不是把 Phase 5 的 solve 函数包了一层循环。

Phase 9 边界：目标测试 PASS 之后的 ``run_regression`` / ``review`` 本轮**没有**
实现，因此最终状态必须是 ``provisionally_verified``，且报告里必须写明回归与复审
未执行。这里显式断言"没有把它伪装成 M2 通过"。
"""

from __future__ import annotations

from pathlib import Path

from harness import (
    CORRECT,
    ISSUE,
    TARGET,
    assert_original_clean,
    build_harness,
    passed_result,
)

from codepilot.graph.routing import Node
from codepilot.schemas.enums import RunStatus
from codepilot.schemas.progress import ProgressSnapshot


def test_first_pass_patch_lands_and_graph_runs(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "fixed normalize"}},
            ]
        ],
    )
    state = harness.run()

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert state.stop_reason is None

    # 补丁真的落在了隔离 Workspace 里。
    assert harness.source() == CORRECT
    assert harness.original_source() == "def normalize(value: int) -> int:\n    return value - 1\n"
    assert_original_clean(harness)

    # 目标测试真的执行过，且只执行了一次（外加基线两次）。
    assert state.execution is not None
    assert state.execution.status.value == "PASS"
    assert len(state.attempts) == 1
    attempt = state.attempts[0]
    assert attempt.validation.passed
    assert attempt.execution is not None
    assert attempt.patch_hash

    # 真的在跑 StateGraph：节点序列包含完整的前置分析与收尾。
    visited = [item["node"] for item in state.events]
    for expected in (
        Node.UNDERSTAND_ISSUE.value,
        Node.RETRIEVE.value,
        Node.LOCALIZE.value,
        Node.ANALYZE_IMPACT.value,
        Node.BUILD_ENV.value,
        Node.REPRODUCE.value,
        Node.PLAN.value,
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
        Node.RUN_REGRESSION.value,
        Node.REVIEW.value,
        Node.FINALIZE.value,
    ):
        assert expected in visited, f"{expected} missing from {visited}"
    assert Node.DIAGNOSE.value not in visited
    assert Node.REPAIR_PATCH.value not in visited


def test_first_pass_does_not_fake_phase9_results(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "fixed normalize"}},
            ]
        ],
    )
    state = harness.run()

    # Phase 9 的产物必须是 None（未测量），不能是"零新增失败"的假对象。
    assert state.regression is None
    assert state.review is None
    assert state.report_artifacts["regression"] == "DEFERRED:phase9-not-implemented"
    assert state.report_artifacts["review"] == "DEFERRED:phase9-not-implemented"
    assert any("regression and review were NOT executed" in item for item in state.failure_reasons)

    # 进度快照也必须声明回归未测量，否则未测量会被读成"0 个新增失败"。
    assert state.current_attempt is not None
    progress = state.current_attempt.progress
    assert isinstance(progress, ProgressSnapshot)
    assert progress.regression_measured is False
    assert progress.target_failed == 0

    report = (harness.artifacts_root / "runs" / state.run_id / "report.md").read_text(
        encoding="utf-8"
    )
    assert "provisionally_verified" in report
    assert "full regression safety" in report


def test_first_pass_budget_accounted_once_per_execution(tmp_path: Path) -> None:
    """基线两次 + 目标测试一次 = 3 次测试执行，全部计入 max_test_runs。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "fixed"}},
            ]
        ],
    )
    state = harness.run()
    assert state.test_runs == 3, state.model_dump_json(indent=2)
    assert [call[2] for call in harness.sandbox.run_calls] == [1, 2, 1]
    assert state.usage.llm_calls == len(harness.llm.calls)
    assert state.usage.total_tokens == state.usage.input_tokens + state.usage.output_tokens


def test_first_pass_writes_artifacts_and_leaves_target_repo_untouched(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result()],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "fixed"}},
            ]
        ],
    )
    state = harness.run()
    run_dir = harness.artifacts_root / "runs" / state.run_id
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "patch_final.diff").is_file()
    assert (run_dir / "state_final.json").is_file()
    assert (run_dir / "attempts" / "01" / "patch.diff").is_file()
    assert "return value" in (run_dir / "patch_final.diff").read_text(encoding="utf-8")
    assert harness.events_path.is_file() and harness.events_path.read_text(encoding="utf-8")
    assert state.issue == ISSUE
    assert state.plan is not None and TARGET in state.plan.tests_to_run
