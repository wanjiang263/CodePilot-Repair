"""ExecutionResult 六态判定与未来图可消费的纯路由。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from codepilot.sandbox.junit import JUnitReport
from codepilot.schemas.enums import ExecutionStatus, TestOutcome

__all__ = [
    "StatusDecision",
    "classify_execution",
    "execution_route",
    "is_third_party_dependency_error",
]

_MISSING_MODULE = re.compile(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]")


class StatusDecision(BaseModel):
    status: ExecutionStatus
    error_reason: str | None = None
    diagnostics: dict[str, object] = Field(default_factory=dict)


def classify_execution(
    *,
    exit_code: int | None,
    junit: JUnitReport | None,
    timed_out: bool = False,
    sandbox_error: str | None = None,
    environment_error: str | None = None,
    oom_killed: bool = False,
    workspace: Path | None = None,
    diagnostic_text: str = "",
) -> StatusDecision:
    """按优先级判定六态；任何矛盾都不会落入 PASS。"""
    if timed_out:
        return StatusDecision(
            status=ExecutionStatus.TIMEOUT,
            error_reason="EXECUTION_TIMEOUT",
            diagnostics={"post_timeout_error": sandbox_error} if sandbox_error else {},
        )
    if oom_killed:
        return StatusDecision(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="OOM_KILLED",
            diagnostics={
                "oom_killed": True,
                **({"cleanup_error": sandbox_error} if sandbox_error else {}),
            },
        )
    if sandbox_error:
        return StatusDecision(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="DOCKER_OR_CONTAINER_ERROR",
            diagnostics={"detail": sandbox_error},
        )
    if environment_error:
        return StatusDecision(
            status=ExecutionStatus.ENV_ERROR,
            error_reason="ENVIRONMENT_BUILD_FAILED",
            diagnostics={"detail": environment_error},
        )
    if is_third_party_dependency_error(diagnostic_text, workspace=workspace):
        return StatusDecision(
            status=ExecutionStatus.ENV_ERROR,
            error_reason="MISSING_THIRD_PARTY_DEPENDENCY",
        )
    if exit_code in {2, 4, 5}:
        return StatusDecision(
            status=ExecutionStatus.COLLECTION_ERROR,
            error_reason="PYTEST_COLLECTION_ERROR",
        )
    if exit_code is None:
        return StatusDecision(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="MISSING_CONTAINER_EXIT_CODE",
        )
    if junit is None:
        return StatusDecision(
            status=ExecutionStatus.SANDBOX_ERROR,
            error_reason="MISSING_OR_INVALID_JUNIT",
        )

    failing = [
        test for test in junit.tests if test.outcome in {TestOutcome.FAILED, TestOutcome.ERROR}
    ]
    if exit_code == 0 and failing:
        return StatusDecision(
            status=ExecutionStatus.FAIL,
            error_reason="EXIT_CODE_JUNIT_CONTRADICTION",
            diagnostics={"failing_junit_cases": len(failing)},
        )
    if exit_code == 0:
        return StatusDecision(status=ExecutionStatus.PASS)
    if exit_code == 1:
        return StatusDecision(status=ExecutionStatus.FAIL)
    return StatusDecision(
        status=ExecutionStatus.SANDBOX_ERROR,
        error_reason="UNEXPECTED_CONTAINER_EXIT",
        diagnostics={"exit_code": exit_code},
    )


def is_third_party_dependency_error(text: str, *, workspace: Path | None) -> bool:
    """仅当缺失模块不是工作区自己的顶层包时才视作 ENV_ERROR。"""
    matches = _MISSING_MODULE.findall(text)
    if not matches or workspace is None:
        return False
    root = Path(workspace)
    for module in matches:
        top_level = module.split(".", 1)[0]
        candidates = (
            root / top_level,
            root / f"{top_level}.py",
            root / "src" / top_level,
            root / "src" / f"{top_level}.py",
        )
        if not any(candidate.exists() for candidate in candidates):
            return True
    return False


def execution_route(
    status: ExecutionStatus,
) -> Literal["finalize", "diagnose", "regression"]:
    """Phase 8 可直接消费的最小纯路由，不提前构建 LangGraph。"""
    if status in {ExecutionStatus.ENV_ERROR, ExecutionStatus.SANDBOX_ERROR}:
        return "finalize"
    if status is ExecutionStatus.PASS:
        return "regression"
    return "diagnose"
