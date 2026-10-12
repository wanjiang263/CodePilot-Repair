"""完全离线、显式标识来源的脚本化 Sandbox。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from pathlib import Path

from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult, TestCaseResult

__all__ = ["FakeSandbox", "scripted_result"]


def scripted_result(
    status: ExecutionStatus | str,
    *,
    tests: Iterable[TestCaseResult] = (),
    exit_code: int | None = None,
) -> ExecutionResult:
    """构造确定性的 Fake 结果，并留下不可混淆的后端标记。"""
    normalized = ExecutionStatus(status)
    cases = list(tests)
    if exit_code is None:
        exit_code = {
            ExecutionStatus.PASS: 0,
            ExecutionStatus.FAIL: 1,
            ExecutionStatus.COLLECTION_ERROR: 2,
        }.get(normalized)
    return ExecutionResult(
        status=normalized,
        exit_code=exit_code,
        tests=cases,
        tests_passed=sum(case.outcome.value == "passed" for case in cases),
        tests_failed=sum(case.outcome.value in {"failed", "error"} for case in cases),
        diagnostics={"execution_backend": "fake", "container_verified": False},
        error_reason="SCRIPTED_FAKE_RESULT",
    )


class FakeSandbox:
    """按给定顺序返回结果；脚本耗尽时失败而非重复最后一次成功。"""

    def __init__(
        self,
        results: Sequence[ExecutionResult],
        *,
        build_status: ExecutionStatus = ExecutionStatus.PASS,
    ) -> None:
        self._results = [deepcopy(result) for result in results]
        self._build_status = build_status
        self.build_calls: list[tuple[str, str, str]] = []
        self.run_calls: list[tuple[str, tuple[str, ...], int]] = []
        self.cleanup_calls: list[int] = []

    @classmethod
    def from_statuses(cls, statuses: Sequence[ExecutionStatus | str]) -> FakeSandbox:
        return cls([scripted_result(status) for status in statuses])

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        del allow_network, env_spec
        self.build_calls.append((repository, base_commit, str(Path(workspace))))
        succeeded = self._build_status is ExecutionStatus.PASS
        return EnvironmentBuildResult(
            status=self._build_status,
            image_ref="fake://environment" if succeeded else None,
            image_id="fake-image" if succeeded else None,
            cache_key="fake-cache-key",
            error=None if succeeded else "scripted environment build failure",
        )

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str] = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult:
        del environment, env_spec
        self.run_calls.append((str(Path(workspace)), tuple(tests), artifact_iteration))
        if not self._results:
            raise RuntimeError("FakeSandbox 脚本已耗尽；拒绝伪造额外执行结果")
        result = deepcopy(self._results.pop(0))
        diagnostics = dict(result.diagnostics)
        diagnostics.update(execution_backend="fake", container_verified=False)
        return result.model_copy(
            update={
                "diagnostics": diagnostics,
                "error_reason": result.error_reason or "SCRIPTED_FAKE_RESULT",
            }
        )

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        self.cleanup_calls.append(older_than_seconds)
        return []
