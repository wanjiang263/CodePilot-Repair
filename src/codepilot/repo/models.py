"""静态分析共用的内存数据模型，与数据库映射解耦。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SymbolKind = Literal["module", "class", "function", "method", "test_function", "test_method"]
Confidence = Literal["high", "medium", "low"]
EdgeKind = Literal["CALL", "IMPORT", "INHERIT", "REFERENCE", "TEST"]

#: 静态分析的三档置信度 ↔ ``edges`` 表的 Float 列。表在 Phase 0 落地为
#: ``Float``（F-12），而 02 §7.1 与内存模型用字符串三档；映射集中在这一处，
#: 索引持久化与图查询都用它，避免两处各自猜数值。
CONFIDENCE_SCORE: dict[Confidence, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}


def confidence_to_score(confidence: Confidence) -> float:
    return CONFIDENCE_SCORE[confidence]


def score_to_confidence(score: float) -> Confidence:
    """把表里的 Float 还原成三档。按最近距离归类，容忍浮点表示误差。"""
    return min(CONFIDENCE_SCORE, key=lambda name: abs(CONFIDENCE_SCORE[name] - score))


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
