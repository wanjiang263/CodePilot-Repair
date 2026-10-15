"""base commit 上两次全量测试生成可审计 TestBaseline。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestBaseline

__all__ = ["build_test_baseline"]


def build_test_baseline(
    sandbox: Sandbox,
    *,
    base_commit: str,
    workspace: Path,
    environment: EnvironmentBuildResult,
    env_spec: EnvSpec,
    first_artifact_iteration: int = 1,
    before_run: Callable[[], EnvSpec | ExecutionResult] | None = None,
) -> TestBaseline:
    """正常情况下固定运行两次；环境/沙箱错误按路由约定立即终止。"""

    def execute(iteration: int) -> ExecutionResult:
        specification = before_run() if before_run is not None else env_spec
        if isinstance(specification, ExecutionResult):
            return specification
        return sandbox.run_tests(
            workspace=workspace,
            environment=environment,
            env_spec=specification,
            artifact_iteration=iteration,
        )

    first = execute(first_artifact_iteration)
    runs = [first]
    if first.status not in {ExecutionStatus.ENV_ERROR, ExecutionStatus.SANDBOX_ERROR}:
        runs.append(execute(first_artifact_iteration + 1))
    mappings = [_outcomes(run) for run in runs]
    all_nodes = sorted(set().union(*(mapping.keys() for mapping in mappings)))
    differences: dict[str, tuple[TestOutcome | None, TestOutcome | None]] = {}
    if len(mappings) == 2:
        for node_id in all_nodes:
            pair = (mappings[0].get(node_id), mappings[1].get(node_id))
            if pair[0] != pair[1]:
                differences[node_id] = pair
    terminal_errors = {
        ExecutionStatus.ENV_ERROR,
        ExecutionStatus.SANDBOX_ERROR,
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.COLLECTION_ERROR,
    }
    complete = not any(run.status in terminal_errors for run in runs)
    evidence = [
        f"run={index + 1} status={run.status.value} artifact={run.artifact_dir or '<none>'}"
        for index, run in enumerate(runs)
    ]
    return TestBaseline(
        base_commit=base_commit,
        outcomes={
            node: mappings[0][node] if node in mappings[0] else mappings[-1][node]
            for node in all_nodes
        },
        flaky=set(differences),
        run_outcomes=mappings,
        status_differences=differences,
        evidence=evidence,
        complete=complete,
        duration_seconds=max(run.duration_seconds for run in runs),
    )


def _outcomes(result: ExecutionResult) -> dict[str, TestOutcome]:
    return {test.node_id: test.outcome for test in result.tests}
