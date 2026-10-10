"""R-17：RepoGraph 的 callers / callees / related_tests。

核心断言是**一致性**：对库里的每一个 symbol，图查询结果与直接 SQL 查询
（in_edges / out_edges 按 kind 过滤）必须相等。图只是持久化结果的视图，
任何一边多一条或少一条都会让两边对不上。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.storage.repository import Stores

FIXTURE_REPOS = Path(__file__).resolve().parents[2] / "fixtures" / "repos"


def _indexed_graph(stores: Stores, tmp_path: Path) -> tuple[RepoGraph, RepositoryIndex]:
    root = tmp_path / "mini_auth"
    shutil.copytree(FIXTURE_REPOS / "mini_auth", root)
    workspace = Workspace(
        path=root,
        source=normalize_source(str(root)),
        base_commit="0" * 40,
        repository_id="repo_test_graph",
    )
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    return RepoGraph(index), index


def test_callers_matches_sql_for_every_symbol(stores: Stores, tmp_path: Path) -> None:
    graph, index = _indexed_graph(stores, tmp_path)
    stores_edges = index.edges(kind="CALL")

    for symbol in index.symbols():
        expected = sorted(e.source_id for e in stores_edges if e.target_id == symbol.id)
        # SQL 与图在同一 (source, target) 上都不会有重复 CALL 边（持久化已去重）。
        actual = sorted(n.symbol.id for n in graph.callers(symbol.qualified_name))
        assert actual == expected, symbol.qualified_name


def test_callees_matches_sql_for_every_symbol(stores: Stores, tmp_path: Path) -> None:
    graph, index = _indexed_graph(stores, tmp_path)
    stores_edges = index.edges(kind="CALL")

    for symbol in index.symbols():
        expected = sorted(
            e.target_id for e in stores_edges if e.source_id == symbol.id and e.target_id
        )
        actual = sorted(n.symbol.id for n in graph.callees(symbol.qualified_name))
        assert actual == expected, symbol.qualified_name


def test_related_tests_matches_sql_for_every_symbol(stores: Stores, tmp_path: Path) -> None:
    graph, index = _indexed_graph(stores, tmp_path)
    stores_edges = index.edges(kind="TEST")

    for symbol in index.symbols():
        expected = sorted(e.source_id for e in stores_edges if e.target_id == symbol.id)
        actual = sorted(n.symbol.id for n in graph.related_tests(symbol.qualified_name))
        assert actual == expected, symbol.qualified_name


def test_known_call_relationships(stores: Stores, tmp_path: Path) -> None:
    graph, _ = _indexed_graph(stores, tmp_path)

    callers = graph.callers("miniauth.tokens.issue_token")
    caller_names = {n.symbol.qualified_name for n in callers}
    assert "miniauth.session.Session.login" in caller_names
    high = next(n for n in callers if n.symbol.qualified_name == "miniauth.session.Session.login")
    assert high.confidence == "high"
    assert high.line > 0

    callees = graph.callees("miniauth.session.Session.login")
    callee_names = {n.symbol.qualified_name for n in callees}
    assert "miniauth.tokens.issue_token" in callee_names

    tests = graph.related_tests("miniauth.tokens.issue_token")
    assert tests, "test_tokens.py 应当静态关联到 issue_token"
    assert all(n.symbol.kind in {"test_function", "test_method"} for n in tests)
    strongest = max(tests, key=lambda n: {"low": 0, "medium": 1, "high": 2}[n.confidence])
    assert strongest.confidence == "high"  # 测试体内有直接调用


def test_empty_result_is_a_list_not_none(stores: Stores, tmp_path: Path) -> None:
    graph, _ = _indexed_graph(stores, tmp_path)
    assert graph.callers("no.such.symbol") == []
    assert graph.callees("no.such.symbol") == []
    assert graph.related_tests("no.such.symbol") == []
    # 存在但没有任何关联的 symbol 同样返回 []。
    assert graph.callers("miniauth.app") == []  # module symbol 无人调用


def test_unresolved_edges_do_not_enter_the_graph(stores: Stores, tmp_path: Path) -> None:
    """target_id 为空的边（hmac.new 这类外部调用）不进图，但留在 SQL 里。"""
    _, index = _indexed_graph(stores, tmp_path)
    edges = index.edges(kind="CALL")
    assert any(e.target_id is None for e in edges)
