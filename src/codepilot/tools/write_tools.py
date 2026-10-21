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
    "validate_test_target",
]


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
            checked = validate_test_target(policy, target)
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


#: 路径部分的字符集：**不允许空格**，也不允许 shell 元字符。node id 部分另说。
_PATH_PART = re.compile(r"^[A-Za-z0-9_./:\-\[\],=+]+$")

#: 单个选择串的长度的上界。真实参数化 ID 可以很长（带引号的邮箱样本约 110 字符），
#: 但一条命令里的选择项必须有界，否则"779 个测试"这种规模下拼出来的命令长度不可控。
_MAX_TARGET_LENGTH = 1024


def validate_test_target(policy: WorkspacePathPolicy, target: str) -> str:
    """把"命令"和"带空格的参数化 NodeID"分开判，而不是看见空格就拒。

    拒的是命令形态：以 ``-`` 开头（会变成 pytest 的 flag）、含控制字符、路径部分越界
    或含空格/元字符、以及超过长度上界。放行的 ``::`` 之后那一段是**pytest 自己打印的
    测试 ID**：真实仓库里存在 ``test_email_invalid[" "@example.org]`` 这种含空格与引号
    的 ID，一律拒掉会让回归阶段在真实基线上直接炸（2026-10-20 首次真实单题运行实测：
    marshmallow 基线 779 条里 9 条是这种 ID）。

    这不是放松注入面：选择项交给沙箱时逐项 ``shlex.quote`` 后才拼进命令
    （:func:`codepilot.sandbox.docker._validate_test_selection` 同样只看 flag、控制字符
    与路径越界），所以空格与引号只会成为测试 ID 的字面内容，不会成为第二个 argv 词。
    """
    if (
        not target
        or target.startswith("-")
        or len(target) > _MAX_TARGET_LENGTH
        or any(character in target for character in "\r\n\t\x00")
    ):
        raise ValueError("test target must be a path or pytest NodeID, never a command")
    path_text = target.split("::", 1)[0].replace("\\", "/")
    if not _PATH_PART.fullmatch(path_text):
        raise ValueError("test target must be a path or pytest NodeID, never a command")
    resolved = policy.resolve(path_text, must_exist=True)
    relative = resolved.relative_to(policy.root).as_posix()
    pure = PurePosixPath(relative)
    if "tests" not in pure.parts and not pure.name.startswith("test_"):
        raise ValueError("test target must identify an existing test file")
    return relative + target[len(path_text) :]


def execution_from_response(response: ToolResponse) -> ExecutionResult | None:
    payload = response.data.get("execution")
    return ExecutionResult.model_validate(payload) if isinstance(payload, dict) else None
