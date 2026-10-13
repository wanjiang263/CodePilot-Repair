"""RepoGraph-backed read-only tools (P-08)."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from codepilot.repo.graph import GraphNeighbor, RepoGraph
from codepilot.tools.base import Tool, ToolPermission, ToolResponse

__all__ = ["GraphQueryInput", "graph_tools"]


class GraphQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qualified_name: str = Field(min_length=1, max_length=500)


def _result(name: str, neighbors: list[GraphNeighbor]) -> ToolResponse:
    relationships = [
        {
            "symbol_id": neighbor.symbol.id,
            "qualified_name": neighbor.symbol.qualified_name,
            "path": neighbor.symbol.path,
            "start_line": neighbor.symbol.start_line,
            "end_line": neighbor.symbol.end_line,
            "kind": neighbor.kind,
            "confidence": neighbor.confidence,
            "edge_line": neighbor.line,
        }
        for neighbor in neighbors
    ]
    return ToolResponse.success(name, relationships=relationships)


def _tool(name: str, description: str, query: Callable[[str], list[GraphNeighbor]]) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = GraphQueryInput.model_validate(raw)
        return _result(name, query(args.qualified_name))

    return Tool(
        name=name,
        description=description,
        permission=ToolPermission.READ,
        input_model=GraphQueryInput,
        handler=handle,
    )


def graph_tools(graph: RepoGraph) -> list[Tool]:
    return [
        _tool("get_callers", "Return indexed direct callers.", graph.callers),
        _tool("get_callees", "Return indexed direct callees.", graph.callees),
        _tool("get_related_tests", "Return indexed related tests.", graph.related_tests),
    ]
