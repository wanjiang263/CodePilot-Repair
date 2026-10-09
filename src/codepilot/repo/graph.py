"""基于持久化索引的 NetworkX 图查询（R-17，接口冻结清单 R 簇）。

图从 ``RepositoryIndex``（即 SQLite 里的 symbols / edges 表）构建，内存中的
NetworkX ``MultiDiGraph`` 只是查询视图——同一对节点之间可以同时存在 CALL、
REFERENCE、INHERIT 等不同 kind 的边，所以必须用 Multi 变体。

两条边界（与 R-09/R-10 的语义一致）：

- ``target_id`` 为空的边（未解析的调用、被抑制的同名方法）**不进图**。
  它们没有可连接的节点，留在 SQL 里供词法通道按 ``target_name`` 使用。
- 查询按 ``qualified_name`` 精确匹配；同名重复定义已由 ``#2`` 后缀消歧，
  一个 qname 至多对应一个 symbol。查不到时返回 ``[]``，不返回 ``None``、
  不抛异常——调用方（检索、影响分析）把"没有"当作正常情况处理。
"""

from __future__ import annotations

from dataclasses import dataclass

# networkx 不自带类型桩（需另装 types-networkx）。项目约定是把这类无桩依赖列进
# pyproject 的 mypy overrides（tree_sitter / rank_bm25 等 9 个都在那里），但本批次
# pyproject.toml 被冻结（属上一轮未提交工作），故在此就地抑制 import-untyped，
# 语义与 overrides 里的 ignore_missing_imports 一致。后续解冻后应移回 overrides。
import networkx as nx  # type: ignore[import-untyped]

from codepilot.repo.index import RepositoryIndex
from codepilot.repo.models import Confidence, EdgeKind, Symbol, score_to_confidence
from codepilot.storage.repository import Stores

__all__ = ["GraphNeighbor", "RepoGraph"]

_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 2, "medium": 1, "low": 0}


@dataclass(frozen=True, slots=True)
class GraphNeighbor:
    """一次图查询的一条结果：相邻 symbol 与连接它的那条边。"""

    symbol: Symbol
    kind: EdgeKind
    confidence: Confidence
    line: int
    """边在**源文件**中的行号。``callers`` 时是调用点的行，``callees`` /
    ``related_tests`` 时是源 symbol 内引用目标的行。"""


class RepoGraph:
    """``callers`` / ``callees`` / ``related_tests`` 查询封装。"""

    def __init__(self, index: RepositoryIndex) -> None:
        self.repository_id = index.repository_id
        self._symbols: dict[str, Symbol] = {}
        self._by_qname: dict[str, str] = {}
        self.graph = nx.MultiDiGraph()
        for symbol in index.symbol_objects():
            self._symbols[symbol.id] = symbol
            self._by_qname[symbol.qualified_name] = symbol.id
            self.graph.add_node(symbol.id)
        for row in index.edges():
            if row.target_id is None or row.target_id not in self._symbols:
                continue
            if row.source_id not in self._symbols:
                continue
            self.graph.add_edge(
                row.source_id,
                row.target_id,
                kind=row.kind,
                confidence=score_to_confidence(row.confidence),
                line=row.line,
            )

    @classmethod
    def from_stores(cls, stores: Stores, repository_id: str) -> RepoGraph:
        return cls(RepositoryIndex(stores, repository_id))

    def callers(self, qname: str) -> list[GraphNeighbor]:
        """谁调用了它（CALL 入边）。"""
        return self._neighbors(qname, "CALL", incoming=True)

    def callees(self, qname: str) -> list[GraphNeighbor]:
        """它调用了谁（CALL 出边，仅已解析目标）。"""
        return self._neighbors(qname, "CALL", incoming=False)

    def related_tests(self, qname: str) -> list[GraphNeighbor]:
        """关联到它的测试（TEST 入边，R-12 的三级关联）。"""
        return self._neighbors(qname, "TEST", incoming=True)

    def _neighbors(self, qname: str, kind: EdgeKind, *, incoming: bool) -> list[GraphNeighbor]:
        symbol_id = self._by_qname.get(qname)
        if symbol_id is None:
            return []
        found: dict[str, GraphNeighbor] = {}
        if incoming:
            pairs = (
                (source, data) for source, _, data in self.graph.in_edges(symbol_id, data=True)
            )
        else:
            pairs = (
                (target, data) for _, target, data in self.graph.out_edges(symbol_id, data=True)
            )
        for other_id, data in pairs:
            if data["kind"] != kind:
                continue
            neighbor = GraphNeighbor(
                self._symbols[other_id], kind, data["confidence"], data["line"]
            )
            previous = found.get(other_id)
            if previous is None or _better(neighbor, previous):
                found[other_id] = neighbor
        return sorted(
            found.values(),
            key=lambda n: (
                n.symbol.qualified_name,
                -_CONFIDENCE_RANK[n.confidence],
                n.line,
            ),
        )


def _better(candidate: GraphNeighbor, previous: GraphNeighbor) -> bool:
    rank = (_CONFIDENCE_RANK[candidate.confidence], -candidate.line)
    return rank > (_CONFIDENCE_RANK[previous.confidence], -previous.line)
