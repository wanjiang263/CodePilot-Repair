from __future__ import annotations

import json
from pathlib import Path

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
    TestRunBudget,
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
        run_tests_tool(policy, sandbox, environment, EnvSpec(), TestRunBudget(2)),
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
    budget = TestRunBudget(1)
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
