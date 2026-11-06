"""§四.2 的 Docker 半边：Coder 撞到每轮 30 次工具上限之后，闭环在**真实容器**里走完。

``tests/graph/test_phase11_closure.py`` 用脚本化 FakeSandbox 把状态机判据钉死；那条
路径上的"测试通过"是脚本给的，证明不了真实 pytest 在真实容器里也给出同一结论。
首次真实运行踩到的两个坑恰好都在容器侧（参数化 NodeID 被当成命令行、JUnit 回读的
ID 还原），所以这里必须把同一条状态机跑到真 daemon 上：

* 编码阶段真的用满 30 次工具调用（不是模拟的一个 stop 标签）；
* 目标测试与回归都由 **DockerSandbox** 在真实容器里执行，结果由真实 JUnit 解析回读；
* PatchReviewer 拿到的是绑定当前补丁哈希的结论；
* 报告里的 closure 逐阶段回答"执行了吗、结果是什么"，并且 ``m2_verified`` 成立。

模型侧仍然是 FakeLLM（``simulated_llm=True``）：本用例证明的是**工程闭环**，
不是模型能力，所以任何结论都不许写成真实模型效果。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import TARGET, build_harness, edit

from codepilot.agents.reviewer import PatchReviewer
from codepilot.artifacts import ArtifactStore
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.routing import Node
from codepilot.graph.stop_rules import patch_hash
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.enums import RunStatus, StopReason

APPROVED: dict[str, object] = {"approved": True, "summary": "reviewed the candidate patch"}
GIVE_UP = {"root_cause": "the edit is still wrong", "repair_strategy": "give_up"}
#: 1 次编辑 + 29 次只读 = 正好用满默认的每轮 30 次工具上限。
READ = {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}}
FINISH = {"tool": "finish", "arguments": {"summary": "done"}}
THIRTY_CALLS: list[dict[str, object]] = [
    edit("src/pkg/core.py", "    return value - 1", "    return value"),
    *[READ] * 29,
]


def _run(tmp_path: Path, monkeypatch, *, case: str):
    """在真实 daemon 上跑一遍完整图；只换补丁形状，其余装配完全一致。"""
    sandbox = DockerSandbox(
        artifacts=ArtifactStore(tmp_path / "docker", case), allowed_workspace_root=tmp_path
    )
    if case == "capped":
        coder_rounds: list[list[dict[str, object]]] = [THIRTY_CALLS]
        diagnoses: list[dict[str, object]] = []
    else:
        coder_rounds = [
            [edit("src/pkg/core.py", "    return value - 1", "    return value - 2"), FINISH]
        ]
        diagnoses = [GIVE_UP]
    harness = build_harness(
        tmp_path,
        results=[],
        baseline_results=[],
        sandbox_override=sandbox,
        coder_rounds=coder_rounds,
        diagnoses=diagnoses,
    )
    # L2 的受影响范围与 Phase 9 的真实 Docker 用例同一给法：impact 提供 related_tests，
    # RegressionRunner 自己按授权清单求交集（不为此放宽任何判据）。
    analyze = harness.deps.impact_analyzer.analyze

    def patched(*args, **kwargs):
        return analyze(*args, **kwargs).model_copy(update={"related_tests": [TARGET]})

    monkeypatch.setattr(harness.deps.impact_analyzer, "analyze", patched)
    reviewer_llm = FakeLLM([APPROVED])
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(reviewer_llm)
    return harness, reviewer_llm


def _report(harness, state) -> dict[str, object]:
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_json"]
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.docker
def test_local_tool_cap_closure_completes_on_real_containers(tmp_path: Path, monkeypatch) -> None:
    """30 次工具上限用满 + 有效补丁 → 真实容器里的 validate → 目标测试 → 回归 → 审查。"""
    harness, reviewer_llm = _run(tmp_path, monkeypatch, case="capped")
    state = harness.run()

    # 前提：局部额度**确实**用满了，而 run 没有被它判死。
    assert len(state.tool_calls) == 30, state.model_dump_json()
    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    visited = [item["node"] for item in state.events]
    for node in (Node.VALIDATE_PATCH, Node.RUN_TARGET_TESTS, Node.RUN_REGRESSION, Node.REVIEW):
        assert node.value in visited, (node.value, visited)

    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.stop_reason is None
    assert state.execution is not None and state.execution.status.value == "PASS"
    assert state.execution.diagnostics.get("execution_backend") == "docker"
    assert state.regression is not None and state.regression.complete
    assert not state.regression.new_failures
    assert all(
        execution.diagnostics.get("execution_backend") == "docker"
        for execution in state.regression.executions.values()
    )
    assert state.review is not None and state.review.approved
    assert state.review.reviewed_patch_hash == patch_hash(state.current_patch)
    assert state.review_calls == 1 and len(reviewer_llm.calls) == 1

    # 真实 JUnit 落盘：结论来自容器里跑出来的 XML，而不是内存里捏的结果。
    assert state.execution.artifact_dir
    junit = harness.deps.artifacts.run_dir / state.execution.artifact_dir / "junit.xml"
    assert junit.is_file(), state.execution.artifact_dir
    text = junit.read_text(encoding="utf-8", errors="replace")
    assert "<testcase" in text and "test_normalize" in text

    report = _report(harness, state)
    closure = report["closure"]  # type: ignore[index]
    assert closure["coding_stop_reason"] == "budget_tool_calls"  # type: ignore[index]
    assert closure["frozen_by_coding_stage"] is True  # type: ignore[index]
    assert closure["candidate_patch_sha256"] == patch_hash(state.current_patch)  # type: ignore[index]
    assert closure["validator_executed"] is True and closure["validator_passed"] is True  # type: ignore[index]
    assert closure["target_tests_executed"] is True and closure["target_tests_status"] == "PASS"  # type: ignore[index]
    assert closure["regression_executed"] is True and closure["regression_complete"] is True  # type: ignore[index]
    assert closure["review_executed"] is True and closure["review_bound_to_candidate"] is True  # type: ignore[index]
    assert closure["unexecuted_stages"] == []  # type: ignore[index]
    assert report["verification_level"] == "m2_verified"
    assert report["simulated_llm"] is True, "本用例只证明工程闭环，模型侧仍是脚本"
    assert harness.deps.accounting.run_log, "必须真的发生过容器执行"
    # 原始仓库分毫不差（03 §28）。
    assert (
        (harness.original / "src" / "pkg" / "core.py")
        .read_text(encoding="utf-8")
        .endswith("    return value - 1\n")
    )


@pytest.mark.docker
def test_real_container_failure_still_refuses_to_bless_the_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    """负控制：真实容器里目标测试**确实失败**时，不许借"补丁在手里"混进审查与成功收尾。

    与上一个用例共用同一条执行路径，只把补丁换成改坏的那一版：如果 Reviewer 会被拿去
    审一个测试都没过的补丁，前面所有闭环断言都只是纸面上的。
    """
    harness, reviewer_llm = _run(tmp_path, monkeypatch, case="failed")
    state = harness.run()

    assert state.execution is not None
    assert state.execution.status.value == "FAIL"
    assert state.execution.diagnostics.get("execution_backend") == "docker"
    assert state.review is None, "测试失败绝不能被审查结论洗白"
    assert reviewer_llm.calls == []
    assert state.status not in {RunStatus.SUCCEEDED, RunStatus.PROVISIONALLY_VERIFIED}

    report = _report(harness, state)
    assert report["verification_level"] != "m2_verified"
    closure = report["closure"]  # type: ignore[index]
    assert closure["target_tests_status"] == "FAIL"  # type: ignore[index]
    assert any(str(item).startswith("review:") for item in closure["unexecuted_stages"])  # type: ignore[index]
