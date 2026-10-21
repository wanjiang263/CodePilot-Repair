from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.patch.diff import generate_git_diff
from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.edit_tools import create_file_tool, edit_file_tool
from codepilot.tools.graph_tools import graph_tools
from codepilot.tools.read_tools import find_symbol_tool, read_file_tool, search_code_tool
from codepilot.tools.registry import ToolRegistry, ToolRole, UnregisteredToolError
from codepilot.tools.write_tools import (
    TestRunBudget as _TestRunBudget,
)
from codepilot.tools.write_tools import (
    finish_tool,
    get_git_diff_tool,
    run_tests_tool,
)


class _EmptyIndex:
    def symbol_objects(self) -> list[object]:
        return []


class _EmptyGraph:
    def callers(self, _name: str) -> list[object]:
        return []

    callees = callers
    related_tests = callers


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_ok.py").write_text("def test_ok():\n    assert 1\n", encoding="utf-8")
    (root / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    run_git(["init", "-q"], cwd=root)
    run_git(["config", "user.email", "tests@example.invalid"], cwd=root)
    run_git(["config", "user.name", "Tests"], cwd=root)
    run_git(["add", "."], cwd=root)
    run_git(["commit", "-qm", "base"], cwd=root)
    return root, run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def test_registry_has_eleven_unique_tools_and_role_permissions(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_ref="fake://env", cache_key="key"
    )
    tools = [
        read_file_tool(policy),
        search_code_tool(policy),
        find_symbol_tool(_EmptyIndex()),  # type: ignore[arg-type]
        *graph_tools(_EmptyGraph()),  # type: ignore[arg-type]
        edit_file_tool(policy),
        create_file_tool(policy),
        get_git_diff_tool(policy, base),
        run_tests_tool(policy, sandbox, environment, EnvSpec(), _TestRunBudget(2)),
        finish_tool(),
    ]
    registry = ToolRegistry(tools)
    assert len(registry.names()) == 11
    assert len(set(registry.names())) == 11
    assert len(registry.names(ToolRole.PLANNER)) == 6
    assert json.dumps(registry.json_schemas())
    try:
        registry.execute("edit_file", {}, role=ToolRole.PLANNER)
    except UnregisteredToolError:
        pass
    else:
        raise AssertionError("planner executed a write tool")


def test_run_tests_accepts_only_existing_test_node_ids_and_counts_budget(tmp_path: Path) -> None:
    root, _ = _repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_ref="fake://env", cache_key="key"
    )
    budget = _TestRunBudget(1)
    tool = run_tests_tool(policy, sandbox, environment, EnvSpec(), budget)
    for target in [
        "tests/test_ok.py;whoami",
        "tests/test_ok.py | other",
        "../tests/test_ok.py",
        "C:/tests/test_ok.py",
        "code.py",
    ]:
        assert not tool.execute({"test_ids": [target]}).ok
    response = tool.execute({"test_ids": ["tests/test_ok.py::test_ok"]})
    assert response.ok and budget.used == 1
    assert sandbox.run_calls[0][1] == ("tests/test_ok.py::test_ok",)
    assert not tool.execute({"test_ids": ["tests/test_ok.py"]}).ok


def test_create_file_cannot_overwrite_or_escape_and_enters_diff(tmp_path: Path) -> None:
    root, base = _repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    tool = create_file_tool(policy)
    assert not tool.execute({"path": "code.py", "content": "bad"}).ok
    assert not tool.execute({"path": "../outside.py", "content": "bad"}).ok
    assert tool.execute({"path": "src/new.py", "content": "VALUE = 2\n"}).ok
    assert "src/new.py" in generate_git_diff(root, base).files


def test_parametrized_node_ids_with_spaces_are_targets_not_commands(
    tmp_path: Path,
) -> None:
    """真实仓库的参数化 ID 里就有空格与引号，拒掉它们等于把回归阶段炸了。

    2026-10-20 首次真实单题运行的实测形状：marshmallow 基线 779 条里 9 条是
    ``test_email_invalid[" "@example.org]`` 这一类，旧判据"看见空格就不是 ID"把它们
    全部当成命令，``run_regression`` 直接抛 ValueError，闭环停在审查之前。
    放行的是 ``::`` 之后的 ID 本体；命令形态仍然一律拒。
    """
    from codepilot.tools.write_tools import validate_test_target

    root, _ = _repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    real_ids = [
        "tests/test_ok.py::test_ok[my message]",
        'tests/test_ok.py::test_email_invalid[" "@example.org]',
        "tests/test_ok.py::test_url_absolute_invalid[ ]",
        'tests/test_ok.py::test_x[a("very.(),:;<>[]".VERY)]',
    ]
    for target in real_ids:
        assert validate_test_target(policy, target).startswith("tests/test_ok.py::")
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_ref="fake://env", cache_key="key"
    )
    tool = run_tests_tool(policy, sandbox, environment, EnvSpec(), _TestRunBudget(1))
    response = tool.execute({"test_ids": ["tests/test_ok.py::test_ok[my message]"]})
    assert response.ok, response.error
    assert sandbox.run_calls[0][1] == ("tests/test_ok.py::test_ok[my message]",)


def test_command_shaped_targets_are_still_rejected(tmp_path: Path) -> None:
    """负控制：放宽的只是 ID 本体的字符集，命令形态一项没放过。"""
    from codepilot.tools.write_tools import validate_test_target

    root, _ = _repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    for target in [
        "",
        "-x",
        "pytest tests/ -x",
        "tests/test_ok.py;rm -rf /",
        "tests/test_ok.py $(id)",
        "tests/test_ok.py::test_ok\nrm -rf /",
        "tests/test_ok.py::test_ok\t",
        "tests/" + "a" * 1200,
    ]:
        with pytest.raises(ValueError, match="never a command"):
            validate_test_target(policy, target)
