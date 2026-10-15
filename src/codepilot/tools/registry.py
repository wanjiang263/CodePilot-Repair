"""Role-scoped tool registry and JSON-schema export (P-12)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from codepilot.llm.types import ToolSpec
from codepilot.tools.base import Tool, ToolPermission, ToolResponse

__all__ = ["ToolRegistry", "ToolRole", "UnregisteredToolError", "build_tool_registry"]

if TYPE_CHECKING:
    from codepilot.repo.graph import RepoGraph
    from codepilot.repo.index import RepositoryIndex
    from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
    from codepilot.sandbox.env_spec import EnvSpec
    from codepilot.tools.base import WorkspacePathPolicy
    from codepilot.tools.write_tools import TestRunBudget


class ToolRole(StrEnum):
    PLANNER = "planner"
    REVIEWER = "reviewer"
    CODER = "coder"


class UnregisteredToolError(LookupError):
    pass


class ToolRegistry:
    PLANNER_NAMES = frozenset(
        {
            "read_file",
            "search_code",
            "find_symbol",
            "get_callers",
            "get_callees",
            "get_related_tests",
        }
    )
    CODER_NAMES = frozenset(
        {
            *PLANNER_NAMES,
            "edit_file",
            "create_file",
            "get_git_diff",
            "run_tests",
            "finish",
        }
    )

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def names(self, role: ToolRole | None = None) -> list[str]:
        allowed = self._allowed(role)
        return sorted(name for name in self._tools if allowed is None or name in allowed)

    def specs(self, role: ToolRole | None = None) -> list[ToolSpec]:
        return [self._tools[name].spec() for name in self.names(role)]

    def json_schemas(self, role: ToolRole | None = None) -> list[dict[str, Any]]:
        return [spec.as_openai_tool() for spec in self.specs(role)]

    def execute(
        self, name: str, arguments: dict[str, Any], *, role: ToolRole | None = None
    ) -> ToolResponse:
        allowed = self._allowed(role)
        tool = self._tools.get(name)
        if tool is None or (allowed is not None and name not in allowed):
            raise UnregisteredToolError(
                f"tool is not registered for {role or 'this registry'}: {name}"
            )
        return tool.execute(arguments)

    def permission(self, name: str) -> ToolPermission:
        try:
            return self._tools[name].permission
        except KeyError as exc:
            raise UnregisteredToolError(name) from exc

    @classmethod
    def _allowed(cls, role: ToolRole | None) -> frozenset[str] | None:
        if role in {ToolRole.PLANNER, ToolRole.REVIEWER}:
            return cls.PLANNER_NAMES
        if role is ToolRole.CODER:
            return cls.CODER_NAMES
        return None


def build_tool_registry(
    *,
    policy: WorkspacePathPolicy,
    index: RepositoryIndex,
    graph: RepoGraph,
    base_commit: str,
    sandbox: Sandbox,
    environment: EnvironmentBuildResult,
    env_spec: EnvSpec,
    test_budget: TestRunBudget,
    authorized_tests: set[str] | None = None,
) -> ToolRegistry:
    """Construct the complete 11-tool contract from injected run dependencies."""
    from codepilot.tools.edit_tools import create_file_tool, edit_file_tool
    from codepilot.tools.graph_tools import graph_tools
    from codepilot.tools.read_tools import find_symbol_tool, read_file_tool, search_code_tool
    from codepilot.tools.write_tools import finish_tool, get_git_diff_tool, run_tests_tool

    return ToolRegistry(
        [
            read_file_tool(policy),
            search_code_tool(policy),
            find_symbol_tool(index),
            *graph_tools(graph),
            edit_file_tool(policy),
            create_file_tool(policy),
            get_git_diff_tool(policy, base_commit),
            run_tests_tool(
                policy,
                sandbox,
                environment,
                env_spec,
                test_budget,
                authorized_tests=authorized_tests,
            ),
            finish_tool(),
        ]
    )
