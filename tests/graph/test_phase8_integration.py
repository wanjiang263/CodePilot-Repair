"""跨阶段集成：Phase 5 → Phase 7 → Phase 8，以及 Phase 6 复现降级。

这条链路要证明的不是"各阶段各自能跑"，而是**它们之间的接口真的接得上**：

Phase 5 的 Coding Agent 真实改坏 Workspace → PatchValidator 真实校验 →
真实 Sandbox 执行 → Phase 7 的解析器/分类器/签名/压缩/DiagnosisAgent 处理**同一份**
``ExecutionResult`` → RepairPolicy 决定生效策略 → Coding Agent 在上一轮的结果上
再改一次 → 通过。

两条硬性红线：

* Diagnosis 只能看到 Issue、base 仓库与本轮证据，**看不到 gold patch**；
* 正确的补丁必须由 Coding Agent 亲手写出来，不能由测试注入。
"""

from __future__ import annotations

from pathlib import Path

from harness import (
    CORRECT,
    DEFECTIVE,
    FINISH,
    assertion_failure,
    build_harness,
    diagnosis,
    passed_result,
)

from codepilot.graph.routing import Node
from codepilot.graph.stop_rules import patch_hash
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RepairStrategy, RunStatus
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY

NODE = "tests/test_core.py::test_normalize"
WRONG = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value - 2",
    },
}
FIX = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 2",
        "replace": "    return value",
    },
}

REPRO_DRAFT = {
    "filename": "test_repro_normalize.py",
    "content": (
        "from pkg.core import normalize\n\n\n"
        "def test_normalize_returns_the_input() -> None:\n    assert normalize(3) == 3\n"
    ),
    "expected_failure": "AssertionError: normalize(3) returns 2 instead of 3",
}
REPRO_NODE = f"{REPRO_TEST_DIRECTORY}/test_repro_normalize.py::test_normalize_returns_the_input"


def _chain(tmp_path: Path):
    return build_harness(
        tmp_path,
        results=[assertion_failure(NODE, message="assert 1 == 3"), passed_result(NODE)],
        coder_rounds=[[WRONG, FINISH], [FIX, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )


# ---------------------------------------------------------------- A. 5 → 7 → 8


def test_full_phase5_to_phase7_to_phase8_chain(tmp_path: Path) -> None:
    harness = _chain(tmp_path)
    state = harness.run()

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.status.value
    assert len(state.attempts) == 2
    first, second = state.attempts

    # Phase 5：补丁由 Coding Agent 真实写出，两轮不同。
    assert first.patch_hash != second.patch_hash
    assert first.patch_hash == patch_hash(first.patch_diff)
    assert harness.source() == CORRECT
    assert harness.original_source() == DEFECTIVE

    # Phase 7：失败经解析→分类→签名→压缩→诊断，产出带证据的 Diagnosis。
    assert first.diagnosis is not None
    assert first.diagnosis.evidence
    assert first.diagnosis.evidence[0].file == "src/pkg/core.py"
    assert first.diagnosis.repair_strategy is RepairStrategy.REFINE
    assert len(first.failure_signatures) == 1

    # 失败信息真的传到下一轮：第 2 轮的 search 块只在第 1 轮落盘后才存在。
    assert [item["name"] for item in state.tool_calls] == ["edit_file", "finish"]
    assert all(item["ok"] for item in state.tool_calls)

    # 报告：通过/未执行都写清楚，不冒充 M2。
    report = (harness.artifacts_root / "runs" / state.run_id / "report.json").read_text(
        encoding="utf-8"
    )
    assert '"provisionally_verified"' in report
    assert '"simulated_llm": true' in report
    assert '"simulated_sandbox": true' in report
    assert "DEFERRED:phase9-not-implemented" in report
    assert "regression safety" in report


def test_diagnosis_never_sees_the_gold_patch(tmp_path: Path) -> None:
    """信息隔离：诊断的 prompt 里不能出现最终正确答案。

    这条断言是整个项目"自主修复"说法的前提。若诊断阶段能读到 gold patch，
    后面所有 resolved rate 都失去意义（03 §11.2）。
    """
    harness = _chain(tmp_path)
    harness.run()

    diagnoser_prompts = [
        text
        for purpose, text in zip(harness.llm.purposes(), harness.llm.prompt_texts(), strict=True)
        if purpose == "diagnoser"
    ]
    assert diagnoser_prompts, "the diagnosis stage never ran"
    for prompt in diagnoser_prompts:
        assert "return value\n" not in prompt
        assert "gold" not in prompt.lower()
        assert "FAIL_TO_PASS" not in prompt
        assert "test_patch" not in prompt


def test_original_repository_is_never_touched(tmp_path: Path) -> None:
    from codepilot.repo.gitcmd import run_git

    harness = _chain(tmp_path)
    harness.run()
    assert run_git(["status", "--porcelain"], cwd=harness.original).stdout == ""
    assert (harness.original / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE


def test_attempt_history_and_artifacts_reconstruct_every_round(tmp_path: Path) -> None:
    harness = _chain(tmp_path)
    state = harness.run()
    run_dir = harness.artifacts_root / "runs" / state.run_id
    for index, _attempt in enumerate(state.attempts, start=1):
        directory = run_dir / "attempts" / f"{index:02d}"
        assert (directory / "patch.diff").is_file()
        assert (directory / "validation.json").is_file()
        assert (directory / "execution.json").is_file()
    assert (run_dir / "attempts" / "01" / "diagnosis.json").is_file()
    assert not (run_dir / "attempts" / "02" / "diagnosis.json").exists()


# ---------------------------------------------------------------- B. Phase 6 降级


def test_validated_reproduction_becomes_the_target_test(tmp_path: Path) -> None:
    """复现成功时：目标测试是复现 NodeID，且临时测试不进入最终补丁。"""
    harness = build_harness(
        tmp_path,
        # 脚本顺序必须对上执行顺序：基线 2 次、复现校验 1 次、两轮目标测试。
        # 少给一条的话，复现校验会"吃掉"第 1 轮的失败结果，第 1 轮反而拿到通过，
        # 整个链路只跑一轮就"成功"了 —— 所以这里同时断言 Attempt 条数。
        results=[
            assertion_failure(REPRO_NODE, message="assert 2 == 3"),
            assertion_failure(REPRO_NODE, message="assert 2 == 3"),
            passed_result(REPRO_NODE),
        ],
        baseline_results=[assertion_failure(NODE, message="assert 2 == 3")] * 2,
        coder_rounds=[[WRONG, FINISH], [FIX, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        with_reproducer=True,
        repro_drafts=[REPRO_DRAFT],
    )
    state = harness.run()

    assert state.reproduction is not None
    assert state.reproduction.status.value == "reproduced"
    assert state.reproduction.test_path == f"{REPRO_TEST_DIRECTORY}/test_repro_normalize.py"
    # 第 1 轮跑的是复现测试，不是 Planner 选的已有测试。
    assert harness.sandbox.run_calls[2][1] == (REPRO_NODE,)
    assert harness.sandbox.run_calls[3][1] == (REPRO_NODE,)

    assert len(state.attempts) == 2, "the loop must really run two rounds"
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json()[:600]
    # 临时复现测试被 diff 层剔除，不进入交付补丁。
    assert REPRO_TEST_DIRECTORY not in state.attempts[-1].patch_diff
    assert "tests/_codepilot_repro" not in state.report_artifacts.get("final_patch", "")
    # 复现验证那次执行计入测试预算：基线 2 + 复现 1 + 目标测试 2 = 5。
    assert state.test_runs == 5


def test_failed_reproduction_degrades_to_existing_tests(tmp_path: Path) -> None:
    """复现不出来时降级到已有测试，流程继续而不是终止。"""
    bogus = {
        "filename": "test_repro_bogus.py",
        # base 上不会失败：validator 判定 invalid_pass，重试后降级。
        "content": "def test_bogus() -> None:\n    assert 1 == 1\n",
        "expected_failure": "AssertionError",
    }
    harness = build_harness(
        tmp_path,
        results=[assertion_failure(NODE, message="assert 1 == 3"), passed_result(NODE)],
        baseline_results=[passed_result(NODE), passed_result(NODE)],
        coder_rounds=[[WRONG, FINISH], [FIX, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        with_reproducer=True,
        repro_drafts=[bogus] * 4,
        budget=RepairBudget(max_iterations=5, max_test_runs=14),
    )
    state = harness.run()

    assert state.reproduction is not None
    assert state.reproduction.status.value == "failed"
    assert state.reproduction.degraded is True
    assert state.reproduction.attempts >= 1
    # 降级后用的确实是 Planner 选出的已有测试。
    assert state.plan is not None and NODE.split("::")[0] in state.plan.tests_to_run
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED
    visited = [item["node"] for item in state.events]
    assert Node.REPRODUCE.value in visited
    assert Node.RUN_TARGET_TESTS.value in visited


def test_reproduction_never_enters_the_final_patch(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[
            assertion_failure(REPRO_NODE, message="assert 2 == 3"),
            assertion_failure(REPRO_NODE, message="assert 2 == 3"),
            passed_result(REPRO_NODE),
        ],
        baseline_results=[assertion_failure(NODE, message="assert 2 == 3")] * 2,
        coder_rounds=[[WRONG, FINISH], [FIX, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        with_reproducer=True,
        repro_drafts=[REPRO_DRAFT],
    )
    state = harness.run()
    assert len(state.attempts) == 2
    for attempt in state.attempts:
        assert REPRO_TEST_DIRECTORY not in attempt.patch_diff
    assert (harness.workspace / REPRO_TEST_DIRECTORY).is_dir(), "复现文件仍在现场供审计"
    assert REPRO_TEST_DIRECTORY not in state.report_artifacts["final_patch"]
