"""Role-scoped tool registry and JSON-schema export (P-12)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from codepilot.llm.types import ToolSpec
from codepilot.tools.base import Tool, ToolPermission, ToolResponse

__all__ = ["ToolRegistry", "ToolRole", "UnregisteredToolError"]


class ToolRole(StrEnum):
    PLANNER = "planner"
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
        if role is ToolRole.PLANNER:
            return cls.PLANNER_NAMES
        if role is ToolRole.CODER:
            return cls.CODER_NAMES
        return None
