"""G-11：Attempt 历史只追加、不覆盖，且在恢复与失败路径下保持一致。

历史是"多轮修复"唯一的审计载体：没有它就无法回答"第 2 轮比第 1 轮好在哪"，
First-Attempt-Resolved / Repair-Gain 这些指标也算不出来。所以这里测的不只是
"有几条"，而是八条容易被破坏的性质：

1. 多轮之后前面的轮次内容逐字不变；
2. 失败的轮次仍然保留；
3. Patch Validation 失败也要有记录；
4. 诊断失败 / 环境错误不清空历史；
5. 原始执行数据能通过 Artifact 找回；
6. 与 ``RepairState`` 的 JSON round-trip 兼容；
7. 恢复重放不会追加出第二个同一轮；
8. 历史里不留未脱敏的秘密。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from harness import (
    FINISH,
    ROUND1_FIXTURE,
    assertion_failure,
    build_harness,
    diagnosis,
    passed_result,
    scripted_result,
)

from codepilot.graph import nodes
from codepilot.graph.stop_rules import patch_hash
from codepilot.schemas.enums import (
    ExecutionStatus,
    RunStatus,
    StopReason,
    TestOutcome,
)
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult


def _type_failure() -> ExecutionResult:
    """第 2 轮换一种**真正不同**的失败。

    如果两轮都是 ``assert 1 == 3`` / ``assert 0 == 3``，Phase 7 的冻结签名会把
    数字归一化成同一个指纹，而失败测试身份也相同，于是 ``repeated_failure`` 在第
    2 轮就合法地终止 —— 那不是三轮历史，是测试想要的路径没走到。
    """
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id="tests/test_core.py::test_normalize",
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type="TypeError",
                message="unsupported operand type(s) for -: 'str' and 'int'",
                traceback="src/pkg/core.py:2: TypeError",
            )
        ],
        exit_code=1,
    )


def _three_round_harness(tmp_path: Path):
    """三轮：坏补丁 → 换一种错误 → 修好。"""
    return build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            _type_failure(),
            passed_result("tests/test_core.py::test_normalize"),
        ],
        coder_rounds=[
            [ROUND1_FIXTURE, FINISH],
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 2",
                        "replace": "    return value - 3",
                    },
                },
                FINISH,
            ],
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 3",
                        "replace": "    return value",
                    },
                },
                FINISH,
            ],
        ],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 3"),
        ],
    )


# ------------------------------------------------------------------ 1 / 2


def test_history_appends_and_keeps_earlier_rounds_byte_identical(tmp_path: Path) -> None:
    harness = _three_round_harness(tmp_path)
    state = harness.run()

    assert [item.iteration for item in state.attempts] == [1, 2, 3]
    hashes = [item.patch_hash for item in state.attempts]
    assert len(set(hashes)) == 3, "three different patches must not collide"

    # 第 1 轮的补丁内容仍然是当时那一份，没有被第 3 轮改写。
    assert "-    return value - 1" in state.attempts[0].patch_diff
    assert "+    return value - 2" in state.attempts[0].patch_diff
    assert "+    return value\n" not in state.attempts[0].patch_diff
    assert "+    return value - 3" in state.attempts[1].patch_diff
    # 最后一轮才是修好的那一份。
    assert "+    return value" in state.attempts[2].patch_diff

    # 失败轮次的执行与诊断都在。
    assert state.attempts[0].execution is not None
    assert state.attempts[0].execution.status.value == "FAIL"
    assert state.attempts[0].diagnosis is not None
    assert state.attempts[2].execution is not None
    assert state.attempts[2].execution.status.value == "PASS"
    assert state.attempts[2].diagnosis is None  # 通过了，没有再诊断


def test_each_round_keeps_its_own_failure_signatures(tmp_path: Path) -> None:
    harness = _three_round_harness(tmp_path)
    state = harness.run()
    assert state.attempts[0].failure_signatures
    assert state.attempts[2].failure_signatures == []


# ------------------------------------------------------------------ 3 / 4


def test_rejected_patch_still_produces_an_attempt(tmp_path: Path) -> None:
    """校验失败也要留记录，否则 validation_loop 数不出来。"""
    harness = build_harness(
        tmp_path,
        results=[],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "pyproject.toml",
                        "search": "name='fixture'",
                        "replace": "name='x'",
                    },
                },
                FINISH,
            ]
        ],
    )
    state = harness.run()
    assert len(state.attempts) >= 1
    rejected = [item for item in state.attempts if not item.validation.passed]
    assert rejected, state.model_dump_json(indent=2)
    assert rejected[0].validation.errors
    assert state.execution is None or state.execution.status.value != "PASS"


def test_environment_error_does_not_clear_history(tmp_path: Path) -> None:
    from codepilot.schemas.enums import ExecutionStatus

    harness = build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
        ],
        coder_rounds=[[ROUND1_FIXTURE, FINISH], [ROUND1_FIXTURE, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )
    # 第二轮的执行结果换成环境问题：必须终止，但第 1 轮历史还在。
    harness.sandbox._results[-1] = harness.sandbox._results[-1].model_copy(
        update={"status": ExecutionStatus.ENV_ERROR, "error_reason": "ENV_BROKEN"}
    )
    state = harness.run()

    assert state.status is RunStatus.SANDBOX_ERROR
    assert state.stop_reason is StopReason.SANDBOX_FAILURE
    assert len(state.attempts) == 2
    assert state.attempts[0].execution is not None


# ------------------------------------------------------------------ 5


def test_raw_execution_data_is_recoverable_from_artifacts(tmp_path: Path) -> None:
    harness = _three_round_harness(tmp_path)
    state = harness.run()
    run_dir = harness.artifacts_root / "runs" / state.run_id
    for attempt in state.attempts:
        patch_file = run_dir / "attempts" / f"{attempt.iteration:02d}" / "patch.diff"
        assert patch_file.is_file()
        assert patch_file.read_text(encoding="utf-8") == attempt.patch_diff
        validation = run_dir / "attempts" / f"{attempt.iteration:02d}" / "validation.json"
        assert json.loads(validation.read_text(encoding="utf-8"))["passed"] is True


# ------------------------------------------------------------------ 6


def test_history_survives_a_json_round_trip(tmp_path: Path) -> None:
    harness = _three_round_harness(tmp_path)
    state = harness.run()
    restored = RepairState.model_validate_json(state.model_dump_json())
    assert restored.attempts == state.attempts
    assert [item.patch_hash for item in restored.attempts] == [
        item.patch_hash for item in state.attempts
    ]
    assert restored.attempts[0].progress == state.attempts[0].progress


# ------------------------------------------------------------------ 7


def test_replaying_validate_patch_does_not_append_a_second_same_round(tmp_path: Path) -> None:
    """中断恢复时同一个节点可能被重跑一次；追加必须是幂等的。"""
    harness = build_harness(
        tmp_path,
        results=[assertion_failure()],
        coder_rounds=[[ROUND1_FIXTURE, FINISH]],
    )
    base = harness.prime(
        patch_diff=ROUND1_DIFF_PLACEHOLDER,
        execution=assertion_failure(),
        diagnosis_payload=_refine_diagnosis(),
    )
    # 清空历史，模拟刚进入 validate_patch 的时刻。
    empty = base.model_copy(update={"attempts": []})
    first = asyncio.run(nodes.validate_patch(empty, harness.deps))
    once = empty.model_copy(update={"attempts": first["attempts"]})

    second = asyncio.run(nodes.validate_patch(once, harness.deps))

    assert len(once.attempts) == 1
    assert len(second["attempts"]) == 1, "replaying validate_patch must not append a second round"
    assert second["attempts"][0].iteration == 1
    # 事件里留下重放痕迹，报告能区分"真跑了一次"和"重放了一次"。
    assert [item for item in second["events"] if item.get("status") == "replayed"]


def _refine_diagnosis():
    from codepilot.schemas.diagnosis import Diagnosis
    from codepilot.schemas.enums import RepairStrategy

    return Diagnosis(root_cause="hypothesis", repair_strategy=RepairStrategy.REFINE)


ROUND1_DIFF_PLACEHOLDER = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value - 2\n"
)


def test_replayed_run_target_tests_reuses_the_recorded_execution(tmp_path: Path) -> None:
    """已有有效证据时不得重新执行 Sandbox，也不能重复计费。"""
    harness = build_harness(
        tmp_path,
        results=[assertion_failure(), assertion_failure()],
        coder_rounds=[[ROUND1_FIXTURE, FINISH]],
    )
    # 先让 prime 跑出合法的 plan / localization。
    state = harness.prime(
        patch_diff=ROUND1_DIFF_PLACEHOLDER,
        execution=assertion_failure(),
        diagnosis_payload=_refine_diagnosis(),
    )
    # 把第 1 轮换成本次 harness 自己的 ArtifactStore 可核验的引用。
    recorded = assertion_failure()
    state = state.model_copy(
        update={
            "current_patch": ROUND1_DIFF_PLACEHOLDER,
            "attempts": [
                Attempt(
                    iteration=1,
                    patch_diff=ROUND1_DIFF_PLACEHOLDER,
                    patch_hash=patch_hash(ROUND1_DIFF_PLACEHOLDER),
                    validation=ValidationResult(passed=True),
                    execution=recorded.model_copy(
                        update={
                            "artifact_dir": harness.deps.artifacts.attempt_relpath(1, "junit.xml")
                        }
                    ),
                    failure_signatures=["sig-a"],
                )
            ],
        }
    )
    harness.deps.artifacts.write_text(
        harness.deps.artifacts.attempt_relpath(1, "junit.xml"), "<testsuites/>"
    )

    runs_before = harness.deps.test_runs
    update = asyncio.run(nodes.run_target_tests(state, harness.deps))

    assert harness.deps.test_runs == runs_before, "a verified execution must not be re-run"
    assert update["execution"].status.value == "FAIL"
    assert [item for item in update["events"] if str(item.get("status", "")).startswith("reused:")]


# ------------------------------------------------------------------ 8


def test_secret_looked_like_content_is_redacted_before_storage(tmp_path: Path) -> None:
    """历史里不能出现未脱敏的秘密；仓库文本里挂着一个 token 也必须被抹掉。"""
    harness = build_harness(
        tmp_path,
        results=[assertion_failure()],
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value  # api_key=sk-abcdefghijklmnopqrstuvwxyz",
                    },
                },
                FINISH,
            ]
        ],
    )
    state = harness.run()
    assert state.attempts, state.model_dump_json()
    blob = json.dumps([item.model_dump(mode="json") for item in state.attempts])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in blob
    assert "***REDACTED***" in blob
