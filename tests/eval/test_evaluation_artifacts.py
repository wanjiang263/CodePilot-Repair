"""评测执行证据保留：``artifact_refs`` 不能再指向已删除的目录。

真实 GLM 批次里 ``results.jsonl`` 的 ``patch_execution`` 指向
``evaluation/runs/<run_id>/attempts/01``，而 evaluator 在 ``finally`` 把整个
run_dir 删掉，证据指针因此悬空。修复后 runs/ 仍然清空（隐藏测试不落盘），
但 ``artifact_refs`` 指向写在 ``evidence/`` 下的脱敏摘要。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codepilot.artifacts import ArtifactStore
from codepilot.eval.evaluation import GoldEvaluator
from codepilot.eval.synthetic import load_fixture
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from support.synth import demo_spec, write_fixture

TARGET = "tests/test_hidden.py::test_normalize_keeps_value"
PASSED_TO_PASS = "tests/test_public.py::test_documented_base_behaviour"


def _case(node: str, outcome: TestOutcome) -> TestCaseResult:
    return TestCaseResult(node_id=node, outcome=outcome, duration=0.01)


def _result(*cases: TestCaseResult) -> ExecutionResult:
    return scripted_result(ExecutionStatus.PASS, tests=list(cases), exit_code=0)


class _ArtifactWritingSandbox(FakeSandbox):
    """在 attempts 目录里落一份 junit，并像真实 sandbox 一样回填 artifact_dir。"""

    def __init__(self, results: list[ExecutionResult], store: ArtifactStore) -> None:
        super().__init__(results)
        self._store = store

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: Any,
        env_spec: Any,
        tests: Any = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult:
        result = super().run_tests(
            workspace=workspace,
            environment=environment,
            env_spec=env_spec,
            tests=tests,
            artifact_iteration=artifact_iteration,
        )
        target = self._store.attempt_dir(artifact_iteration)
        (target / "junit.xml").write_text("<testsuite/>", encoding="utf-8")
        return result.model_copy(update={"artifact_dir": str(target)})


def test_baseline_artifact_ref_survives_cleanup(tmp_path: Path) -> None:
    def factory(artifacts: ArtifactStore) -> Sandbox:
        return _ArtifactWritingSandbox(
            [
                _result(
                    _case(TARGET, TestOutcome.FAILED),
                    _case(PASSED_TO_PASS, TestOutcome.PASSED),
                )
            ],
            artifacts,
        )

    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=factory,
        work_root=tmp_path / "work",
    )
    fixture = load_fixture(write_fixture(tmp_path, demo_spec(), name="eval"))
    baseline = evaluator.baseline(fixture, run_id="run")
    ref = baseline.artifact_refs["baseline_execution"]
    evidence = Path(ref)
    assert evidence.is_file()
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["tests_observed"] == 2
    # 评测 run 目录仍然必须被清空（不能把隐藏测试 workspace/junit 留在磁盘上）。
    assert not (tmp_path / "work" / "runs" / "run-baseline").exists()
