"""DA-024 的真实容器半边：闭环准入在 **真实 Docker daemon** 上的两条路。

脚本化沙箱能钉住状态机判据，但"剩余次数够不够"这句话最终要对的是**真容器执行次数**：
``AccountingSandbox`` 数的就是这些调用，而真实 pytest 的耗时、镜像与 JUnit 回读都可能
改变一个 run 能走多远。glm14 恰好是在真实基线（779 条授权 NodeID）上撞满 12 次的，
所以这里把同一份判据搬到真 daemon 上跑两遍：

* **不足**：拒绝启动第二轮，且拒绝之后**一次容器都不再多跑**——已挣到的目标测试与
  L2/L3 证据、绑定当前补丁的审查结论必须原样留在报告里；
* **刚好够**：放行之后第二轮必须在真容器里把目标测试、L2、L3 与复审全部走完，
  审查结论绑定第二版补丁，``m2_verified`` 成立。

模型侧仍是 FakeLLM（``simulated_llm=True``）：这两条证明的是**工程闭环与额度判定**，
不是模型修复能力，任何结论都不许写成真实模型效果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from harness import TARGET, build_harness, edit

from codepilot.agents.reviewer import PatchReviewer
from codepilot.artifacts import ArtifactStore
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.routing import Node
from codepilot.graph.stop_rules import patch_hash
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason

APPROVED: dict[str, object] = {"approved": True, "summary": "reviewed the candidate patch"}
HIGH: dict[str, object] = {
    "approved": False,
    "issues": [
        {
            "severity": "high",
            "file": "src/pkg/core.py",
            "line": 2,
            "description": "The identity contract still needs an explicit assertion",
        }
    ],
}
FINISH: dict[str, Any] = {"tool": "finish", "arguments": {"summary": "done"}}
ROUND_A: list[dict[str, Any]] = [
    edit("src/pkg/core.py", "    return value - 1", "    return value"),
    FINISH,
]
ROUND_B: list[dict[str, Any]] = [
    edit("src/pkg/core.py", "    return value", "    return int(value)"),
    FINISH,
]


def _run(tmp_path: Path, monkeypatch, *, case: str):
    """在真实 daemon 上跑完整图；只换额度与补丁形状，其余装配一致。"""
    sandbox = DockerSandbox(
        artifacts=ArtifactStore(tmp_path / "docker", case), allowed_workspace_root=tmp_path
    )
    coder_rounds = [ROUND_A] if case == "refused" else [ROUND_A, ROUND_B]
    reviewer_replies = [HIGH, APPROVED]
    harness = build_harness(
        tmp_path,
        results=[],
        baseline_results=[],
        sandbox_override=sandbox,
        coder_rounds=coder_rounds,
        budget=RepairBudget(max_test_runs=7 if case == "refused" else 8),
    )
    # L2 的受影响范围与 Phase 9/11 的真实 Docker 用例同一给法：impact 提供 related_tests，
    # RegressionRunner 自己按授权清单求交集（不为此放宽任何判据）。
    analyze = harness.deps.impact_analyzer.analyze

    def patched(*args: Any, **kwargs: Any):
        return analyze(*args, **kwargs).model_copy(update={"related_tests": [TARGET]})

    monkeypatch.setattr(harness.deps.impact_analyzer, "analyze", patched)
    reviewer_llm = FakeLLM(reviewer_replies)
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(reviewer_llm)
    return harness, reviewer_llm


def _report(harness: Any, state: Any) -> dict[str, Any]:
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_json"]
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.docker
def test_a_refused_round_runs_no_further_container_on_real_docker(
    tmp_path: Path, monkeypatch
) -> None:
    """剩余 2 次而闭环要 3 次 → 真容器上第二轮不启动，容器执行次数停在 5。"""
    harness, reviewer_llm = _run(tmp_path, monkeypatch, case="refused")
    state = harness.run()

    visited = [item["node"] for item in state.events]
    assert Node.REVIEW.value in visited and Node.REPAIR_PATCH.value in visited

    # 前提：第一轮确实在真容器里挣到了完整闭环（不是脚本给的 PASS）。
    assert harness.deps.accounting.run_log and len(harness.deps.accounting.run_log) == 5, (
        state.model_dump_json()
    )
    assert state.execution is not None
    assert state.execution.status.value == "PASS"
    assert state.execution.diagnostics.get("execution_backend") == "docker"
    assert state.regression is not None and state.regression.complete
    assert all(
        execution.diagnostics.get("execution_backend") == "docker"
        for execution in state.regression.executions.values()
    )
    assert len(reviewer_llm.calls) == 1

    # 判定：不启动第二轮，并且**一次容器都不再多跑**。
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert state.stop_reason is StopReason.BUDGET_CLOSURE_TEST_RUNS
    assert harness.deps.accounting.run_log and len(harness.deps.accounting.run_log) == 5
    assert len(reviewer_llm.calls) == 1
    assert len(state.attempts) == 1
    assert state.review_repairs == 0
    admission = state.closure_admission
    assert admission is not None and admission.feasible is False
    assert admission.required == 3 and admission.remaining == 2
    assert state.frozen_patch_hash == patch_hash(state.current_patch)

    report = _report(harness, state)
    closure = report["closure"]
    assert closure["review_executed"] is True
    assert closure["review_bound_to_candidate"] is True
    assert closure["regression_complete"] is True
    assert closure["review_high_issues"] == 1
    assert any(str(item).startswith("repair_round:") for item in closure["unexecuted_stages"])
    assert report["verification_level"] == "budget_exhausted"
    assert report["simulated_llm"] is True, "本用例只证明额度判定与工程闭环"
    # 原始仓库分毫不差（03 §28）。
    assert (
        (harness.original / "src" / "pkg" / "core.py")
        .read_text(encoding="utf-8")
        .endswith("    return value - 1\n")
    )


@pytest.mark.docker
def test_a_round_with_just_enough_room_closes_on_real_containers(
    tmp_path: Path, monkeypatch
) -> None:
    """负控制：同一形状只多一个计数单位（7 → 8）时，第二轮必须在真容器里走完闭环。

    这条与上一条一起排除两种假阳性：把准入写成"永远拒绝"（那上一条也会"过"），或
    把准入写成"永远放行"（那这一条的新候选就会以未完成的回归混进 ``m2_verified``）。
    """
    harness, reviewer_llm = _run(tmp_path, monkeypatch, case="allowed")
    state = harness.run()

    assert len(harness.deps.accounting.run_log) == 8, state.model_dump_json()
    assert harness.llm.purposes().count("coder") == 4
    assert len(reviewer_llm.calls) == 2, "复审重新问了模型，没有复用旧结论"

    admission = state.closure_admission
    assert admission is not None and admission.feasible is True
    assert admission.remaining == admission.required == 3
    assert admission.coder_share == 0

    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert len(state.attempts) == 2
    first, second = state.attempts
    assert second.patch_hash != first.patch_hash
    assert second.review is not None
    assert second.review.reviewed_patch_hash == second.patch_hash == patch_hash(state.current_patch)

    report = _report(harness, state)
    assert report["verification_level"] == "m2_verified"
    closure = report["closure"]
    assert closure["unexecuted_stages"] == []
    assert closure["post_review_repair"] is True
    assert closure["review_bound_to_candidate"] is True
