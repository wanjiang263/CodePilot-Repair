"""FakeLLM + **真实 DockerSandbox** 的两轮修复 E2E。

边界必须说清楚，这是本文件存在的唯一理由：

* **LLM 是假的**：脚本化的规划 / 编码 / 诊断回复，不代表任何真实模型的自主修复
  能力。那条验收仍然是 P-24 / DA-005 / DA-008 的 NOT RUN，不能因为这里通过就关闭。
* **执行是真的**：两轮都在 Linux 容器里真跑 pytest。第 1 轮真的失败、第 2 轮真的
  通过，JUnit 与失败签名都来自容器输出，而不是脚本返回值。

所以这条链路证明的是"多轮修复闭环在真实执行下接得上"，不是"模型会修 bug"。

环境镜像由 ``build_harness`` 自己构建（允许网络），因此不依赖跨用例的 session
夹具；一次构建 + 三次测试执行，是这条验收的全部容器成本。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import DEFECTIVE, FINISH, build_harness, diagnosis

from codepilot.artifacts import ArtifactStore
from codepilot.failure.parser import FailureParser
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY

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


@pytest.mark.docker
def test_fake_llm_with_real_docker_repairs_over_two_rounds(tmp_path: Path) -> None:
    artifacts = ArtifactStore(tmp_path / "docker-artifacts", "phase8-docker-two-round")
    sandbox = DockerSandbox(artifacts=artifacts, allowed_workspace_root=tmp_path)
    harness = build_harness(
        tmp_path,
        # 真实后端：不脚本化任何执行结果，每一轮都由容器自己给出结论。
        results=[],
        baseline_results=[],
        coder_rounds=[[WRONG, FINISH], [FIX, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        sandbox_override=sandbox,
        budget=RepairBudget(max_iterations=5, max_test_runs=12),
    )
    assert harness.deps.environment.succeeded, harness.deps.environment.error

    state = harness.run()

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.status.value
    assert len(state.attempts) == 2
    first, second = state.attempts

    # 两轮都是真容器执行。
    assert first.execution is not None and second.execution is not None
    assert first.execution.diagnostics["execution_backend"] == "docker"
    assert first.execution.diagnostics["container_verified"] is True
    assert second.execution.diagnostics["execution_backend"] == "docker"
    assert first.execution.status is ExecutionStatus.FAIL
    assert second.execution.status is ExecutionStatus.PASS
    # 基线 2 次 + 两轮目标测试 = 4 次真实容器执行，全部计入测试预算。
    assert state.test_runs == 4
    assert harness.deps.test_runs == 4

    # 失败证据来自容器输出：JUnit 真实落盘，Phase 7 能从中解析出结构化失败。
    assert first.failure_signatures
    junit_files = sorted(artifacts.run_dir.rglob("junit.xml"))
    assert junit_files, "容器没有产出任何 JUnit 证据"
    parsed = FailureParser(repo_files={"tests/test_core.py", "src/pkg/core.py"}).parse_xml(
        junit_files[0].read_text(encoding="utf-8"), artifact_ref=junit_files[0].name
    )
    assert parsed.failures
    assert parsed.failures[0].exception_type == "AssertionError"

    # 诊断真的读过第 1 轮的失败与补丁。
    assert first.diagnosis is not None
    assert first.diagnosis.evidence

    # 补丁由 Coding Agent 真实写出，两轮不同；最终文件是被修好的那一版。
    assert first.patch_hash != second.patch_hash
    assert (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8") == (
        "def normalize(value: int) -> int:\n    return value\n"
    )

    # 原始仓库保持干净；交付补丁里没有复现目录。
    from codepilot.repo.gitcmd import run_git

    assert run_git(["status", "--porcelain"], cwd=harness.original).stdout == ""
    assert harness.original_source() == DEFECTIVE
    assert REPRO_TEST_DIRECTORY not in second.patch_diff

    # 报告如实标注边界：LLM 是模拟的，容器是真验证过的，回归与复审没做。
    # 报告写在 harness 自己的 ArtifactStore 里，容器输出写在传给 DockerSandbox 的那份，
    # 两个目录都要能落到，否则现场不完整。
    report_dir = harness.artifacts_root / "runs" / state.run_id
    assert (report_dir / "report.json").is_file()
    report = (report_dir / "report.json").read_text(encoding="utf-8")
    assert '"simulated_llm": true' in report
    assert '"simulated_sandbox": false' in report
    assert "DEFERRED:phase9-not-implemented" in report
