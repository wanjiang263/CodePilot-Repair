"""Git diff, sandboxed test and finish tools (P-11)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from codepilot.patch.diff import generate_git_diff
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.execution import ExecutionResult
from codepilot.tools.base import EmptyInput, Tool, ToolPermission, ToolResponse, WorkspacePathPolicy

__all__ = [
    "FinishInput",
    "RunTestsInput",
    "TestRunBudget",
    "finish_tool",
    "get_git_diff_tool",
    "run_tests_tool",
]

_NODE_ID = re.compile(r"^[A-Za-z0-9_./:\-\[\],=+]+$")


class RunTestsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_ids: list[str] = Field(min_length=1, max_length=100)


class FinishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)


@dataclass(slots=True)
class TestRunBudget:
    maximum: int
    used: int = 0


def get_git_diff_tool(policy: WorkspacePathPolicy, base_commit: str) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        EmptyInput.model_validate(raw)
        result = generate_git_diff(policy.root, base_commit, policy=policy)
        return ToolResponse.success("get_git_diff", **result.model_dump(mode="json"))

    return Tool(
        name="get_git_diff",
        description="Return the canonical workspace diff against the fixed base commit.",
        permission=ToolPermission.READ,
        input_model=EmptyInput,
        handler=handle,
    )


def run_tests_tool(
    policy: WorkspacePathPolicy,
    sandbox: Sandbox,
    environment: EnvironmentBuildResult,
    env_spec: EnvSpec,
    budget: TestRunBudget,
    *,
    authorized_tests: set[str] | None = None,
) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = RunTestsInput.model_validate(raw)
        if budget.used >= budget.maximum:
            return ToolResponse.failure(
                "run_tests", "budget_exhausted", "test-run budget exhausted"
            )
        normalized: list[str] = []
        for target in args.test_ids:
            checked = _validate_test_target(policy, target)
            if authorized_tests is not None and checked not in authorized_tests:
                return ToolResponse.failure(
                    "run_tests", "unauthorized_test", f"test is not authorized: {target}"
                )
            normalized.append(checked)
        budget.used += 1
        result = sandbox.run_tests(
            workspace=policy.root,
            environment=environment,
            env_spec=env_spec,
            tests=normalized,
            artifact_iteration=budget.used,
        )
        return ToolResponse.success(
            "run_tests",
            test_run=budget.used,
            execution=result.model_dump(mode="json"),
        )

    return Tool(
        name="run_tests",
        description="Run authorized existing pytest targets in the injected sandbox.",
        permission=ToolPermission.EXECUTE,
        input_model=RunTestsInput,
        handler=handle,
    )


def finish_tool() -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = FinishInput.model_validate(raw)
        return ToolResponse.success("finish", summary=args.summary, requested=True)

    return Tool(
        name="finish",
        description="End the coding tool loop; this does not imply validation or test success.",
        permission=ToolPermission.READ,
        input_model=FinishInput,
        handler=handle,
    )


def _validate_test_target(policy: WorkspacePathPolicy, target: str) -> str:
    if (
        not target
        or target.startswith("-")
        or not _NODE_ID.fullmatch(target)
        or any(character in target for character in ";|&><`$(){}!\r\n\t ")
    ):
        raise ValueError("test target must be a path or pytest NodeID, never a command")
    path_text = target.split("::", 1)[0].replace("\\", "/")
    resolved = policy.resolve(path_text, must_exist=True)
    relative = resolved.relative_to(policy.root).as_posix()
    pure = PurePosixPath(relative)
    if "tests" not in pure.parts and not pure.name.startswith("test_"):
        raise ValueError("test target must identify an existing test file")
    return relative + target[len(path_text) :]


def execution_from_response(response: ToolResponse) -> ExecutionResult | None:
    payload = response.data.get("execution")
    return ExecutionResult.model_validate(payload) if isinstance(payload, dict) else None
