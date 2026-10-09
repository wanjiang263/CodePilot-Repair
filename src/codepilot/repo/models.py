"""静态分析共用的内存数据模型，与数据库映射解耦。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SymbolKind = Literal["module", "class", "function", "method", "test_function", "test_method"]
Confidence = Literal["high", "medium", "low"]
EdgeKind = Literal["CALL", "IMPORT", "INHERIT", "REFERENCE", "TEST"]


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    qualified_name: str
    kind: SymbolKind
    id: str = ""
    file_id: str = ""
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    signature: str | None = None
    docstring: str | None = None
    decorators: tuple[str, ...] = ()
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    source_id: str
    target_id: str | None
    target_name: str
    kind: EdgeKind
    confidence: Confidence
    line: int


@dataclass(frozen=True, slots=True)
class ImportBinding:
    source_id: str
    module: str
    name: str | None
    alias: str | None
    bound_name: str
    target_module: str | None
    target_ids: tuple[str, ...]
    external: bool
    line: int
    wildcard: bool = False
