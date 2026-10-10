"""V-11：结构通道以种子分沿 CALL / INHERIT / IMPORT 按深度 0.5^d 扩展。

期望值全部写成精确数字：衰减与 min-max 归一化都是纯算术，写成"约等于"会让
0.25 变成 0.3 这类真实错误溜过去。
"""

import shutil
from pathlib import Path

import pytest

from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.documents import RetrievalError, build_documents
from codepilot.retrieval.lexical import BM25Channel
from codepilot.retrieval.query import build_query
from codepilot.retrieval.service import retrieve_symbols
from codepilot.retrieval.structural import StructuralChannel
from codepilot.schemas.retrieval import RetrievalResult
from support.retrieval import FakeGraph, document, seed

FIXTURE_REPOS = Path(__file__).resolve().parents[2] / "fixtures" / "repos"


def _chain(*ids):
    """``a -> b -> c`` 的 CALL 边链；每个符号同时是一份文档。"""
    docs = [document(sid, f"{sid} body", f"{sid}.py", index + 1) for index, sid in enumerate(ids)]
    edges = [
        (docs[index].symbol, docs[index + 1].symbol, "CALL", "high", 1)
        for index in range(len(docs) - 1)
    ]
    return docs, edges


def test_decay_is_exactly_seed_score_times_half_to_the_depth():
    docs, edges = _chain("s", "one", "two")
    found = StructuralChannel(FakeGraph(edges), docs).search([seed("s", 1.0)])
    assert [c.symbol_id for c in found] == ["one", "two"]
    assert [c.score for c in found] == [0.5, 0.25]
    assert [c.channel_scores[0].raw for c in found] == [0.5, 0.25]
    assert [c.channel_scores[0].normalized for c in found] == [1.0, 0.0]
    assert all(c.channel_scores[0].channel == "structural" for c in found)


def test_depth_three_produces_no_candidates_and_is_never_queried():
    docs, edges = _chain("s", "one", "two", "three")
    graph = FakeGraph(edges)
    found = StructuralChannel(graph, docs).search([seed("s", 1.0)])
    assert [c.symbol_id for c in found] == ["one", "two"]
    # 不只是被丢弃：深度 2 的节点根本不会再被展开。
    assert graph.queries == ["s", "one"]
    deep = StructuralChannel(graph, docs, max_depth=3).search([seed("s", 1.0)])
    assert [c.score for c in deep] == [0.5, 0.25, 0.125]


def test_shortest_path_wins_regardless_of_edge_order():
    docs = {doc.symbol.id: doc for doc in _chain("s", "x", "y")[0]}
    direct = (docs["s"].symbol, docs["x"].symbol, "CALL", "high", 1)
    detour = (docs["s"].symbol, docs["y"].symbol, "CALL", "high", 2)
    back = (docs["y"].symbol, docs["x"].symbol, "CALL", "high", 3)
    ordered = [docs[key] for key in ("s", "x", "y")]
    for edges in ([direct, detour, back], [detour, back, direct], [back, direct, detour]):
        found = StructuralChannel(FakeGraph(edges), ordered).search([seed("s", 1.0)])
        assert {c.symbol_id: c.score for c in found} == {"x": 0.5, "y": 0.5}


def test_only_call_inherit_and_import_are_traversed():
    docs = [document(sid, sid, f"{sid}.py") for sid in ("s", "call", "inherit", "test", "ref")]
    by_id = {doc.symbol.id: doc for doc in docs}
    edges = [
        (by_id["s"].symbol, by_id["call"].symbol, "CALL", "high", 1),
        (by_id["s"].symbol, by_id["inherit"].symbol, "INHERIT", "high", 2),
        (by_id["s"].symbol, by_id["test"].symbol, "TEST", "high", 3),
        (by_id["s"].symbol, by_id["ref"].symbol, "REFERENCE", "high", 4),
    ]
    channel = StructuralChannel(FakeGraph(edges), docs)
    assert {c.symbol_id for c in channel.search([seed("s", 1.0)])} == {"call", "inherit"}
    only_tests = StructuralChannel(FakeGraph(edges), docs, kinds=("TEST",))
    assert {c.symbol_id for c in only_tests.search([seed("s", 1.0)])} == {"test"}


def test_expansion_walks_incoming_edges_too():
    docs, edges = _chain("callee", "caller")
    found = StructuralChannel(FakeGraph(edges), docs).search([seed("caller", 0.8)])
    assert [(c.symbol_id, c.score) for c in found] == [("callee", 0.4)]


def test_a_seed_is_not_reemitted_as_its_own_depth_zero_candidate():
    docs, edges = _chain("s", "one")
    channel = StructuralChannel(FakeGraph(edges), docs)
    assert [c.symbol_id for c in channel.search([seed("s", 1.0)])] == ["one"]
    assert channel.search([seed("s", 0.0)]) == []
    assert channel.search([seed("s", -0.8)]) == []
    assert channel.search([]) == []


def test_a_seed_reached_through_an_edge_is_genuine_structural_evidence():
    docs, edges = _chain("first", "second", "third")
    found = StructuralChannel(FakeGraph(edges), docs).search(
        [seed("first", 1.0), seed("second", 0.8)]
    )
    # second 是种子，但它同时是 first 的深度 1 邻居；first 也能经 second 的反向边到达。
    assert {c.symbol_id: c.score for c in found} == {"second": 0.5, "third": 0.4, "first": 0.4}


def test_seed_limit_takes_the_head_of_the_upstream_ranking():
    docs, edges = _chain("s", "one")
    late = document("late", "late body", "late.py", 3)
    tail = document("tail", "tail body", "tail.py", 7)
    graph = FakeGraph([*edges, (late.symbol, tail.symbol, "CALL", "high", 2)])
    candidates = [*docs, late, tail]
    seeds = [seed("s", 1.0), seed("late", 0.9)]
    one_only = StructuralChannel(graph, candidates, seed_limit=1).search(seeds)
    assert [(c.symbol_id, c.score) for c in one_only] == [("one", 0.5)]
    both = StructuralChannel(graph, candidates).search(seeds)
    assert [(c.symbol_id, c.score) for c in both] == [("one", 0.5), ("tail", 0.45)]


def test_seeds_outside_the_candidate_set_and_isolated_seeds_are_skipped():
    docs, edges = _chain("s", "one")
    isolated = document("iso", "iso body", "iso.py", 9)
    channel = StructuralChannel(FakeGraph(edges), [*docs, isolated])
    assert channel.search([seed("unknown", 1.0)]) == []
    assert channel.search([seed("iso", 1.0)]) == []  # 图上没有边不是错误
    assert [c.symbol_id for c in channel.search([seed("unknown", 1.0), seed("s", 1.0)])] == ["one"]


def test_symbols_without_a_document_are_traversed_but_not_reported():
    docs = [document("s", "s", "s.py"), document("far", "far", "far.py")]
    by_id = {doc.symbol.id: doc for doc in docs}
    hub = document("hub", "hub", "hub.py").symbol
    edges = [
        (by_id["s"].symbol, hub, "CALL", "high", 1),
        (hub, by_id["far"].symbol, "IMPORT", "high", 2),
    ]
    found = StructuralChannel(FakeGraph(edges), docs).search([seed("s", 1.0)])
    assert [(c.symbol_id, c.score) for c in found] == [("far", 0.25)]


def test_strongest_seed_wins_and_result_is_seed_order_independent():
    docs = [
        document("s1", "s1", "a.py", 1),
        document("s2", "s2", "b.py", 1),
        document("shared", "shared", "c.py", 1),
        document("weak", "weak", "d.py", 1),
    ]
    by_id = {doc.symbol.id: doc for doc in docs}
    edges = [
        (by_id["s1"].symbol, by_id["shared"].symbol, "CALL", "high", 1),
        (by_id["s2"].symbol, by_id["shared"].symbol, "CALL", "high", 1),
        (by_id["s2"].symbol, by_id["weak"].symbol, "IMPORT", "high", 2),
    ]
    channel = StructuralChannel(FakeGraph(edges), docs)
    forward = channel.search([seed("s1", 1.0), seed("s2", 0.6)])
    backward = channel.search([seed("s2", 0.6), seed("s1", 1.0)])
    assert forward == backward
    # shared 取两个种子里更强的 1.0*0.5；s1/s2 也能通过对方的边被 reach 到。
    assert {c.symbol_id: c.score for c in forward} == {
        "shared": 0.5,
        "weak": 0.3,
        "s2": 0.25,
        "s1": 0.15,
    }
    assert [c.symbol_id for c in forward] == ["shared", "weak", "s2", "s1"]


def test_equal_scores_order_by_file_start_line_qualified_name_id():
    docs = [
        document("s", "s", "a.py", 1),
        document("z", "z", "z.py", 9),
        document("later", "b", "z.py", 1),
        document("dup", "b", "z.py", 1),
        document("b", "b", "z.py", 1),
    ]
    by_id = {doc.symbol.id: doc for doc in docs}
    edges = [
        (by_id["s"].symbol, by_id[key].symbol, "CALL", "high", 1)
        for key in ("z", "later", "dup", "b")
    ]
    found = StructuralChannel(FakeGraph(edges), docs).search([seed("s", 1.0)])
    assert [c.symbol_id for c in found] == ["b", "dup", "later", "z"]
    assert {c.score for c in found} == {0.5}
    assert [c.channel_scores[0].normalized for c in found] == [1.0, 1.0, 1.0, 1.0]


def test_normalization_uses_the_full_candidate_set_before_top_k():
    docs, edges = _chain("s", "one", "two", "three")
    channel = StructuralChannel(FakeGraph(edges), docs, max_depth=3)
    full = channel.search([seed("s", 1.0)])
    assert [c.score for c in full] == [0.5, 0.25, 0.125]
    assert [c.channel_scores[0].normalized for c in full] == pytest.approx([1.0, 1 / 3, 0.0])
    head = channel.search([seed("s", 1.0)], limit=2)
    assert head == full[:2]
    assert [c.channel_scores[0].normalized for c in head] == [
        c.channel_scores[0].normalized for c in full[:2]
    ]


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit(limit):
    docs, edges = _chain("s", "one")
    with pytest.raises(ValueError):
        StructuralChannel(FakeGraph(edges), docs).search([seed("s", 1.0)], limit=limit)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed_limit": 0},
        {"seed_limit": -1},
        {"max_depth": 0},
        {"decay": 0},
        {"decay": -0.5},
        {"decay": 1.5},
        {"kinds": ()},
    ],
)
def test_invalid_configuration(kwargs):
    docs, edges = _chain("s", "one")
    with pytest.raises(ValueError):
        StructuralChannel(FakeGraph(edges), docs, **kwargs)


def test_duplicate_or_missing_ids_rejected():
    docs, edges = _chain("s", "one")
    for bad in ([*docs, document("s", "dup", "d.py")], [*docs, document("", "x", "e.py")]):
        with pytest.raises(RetrievalError, match="unique nonempty"):
            StructuralChannel(FakeGraph(edges), bad)


def _indexed(tmp_path: Path, stores):
    root = tmp_path / "mini_auth"
    shutil.copytree(FIXTURE_REPOS / "mini_auth", root)
    workspace = Workspace(
        path=root,
        source=normalize_source(str(root)),
        base_commit="0" * 40,
        repository_id="repo_structural",
    )
    Indexer().index(stores, workspace)
    symbols = RepositoryIndex(stores, workspace.repository_id).symbol_objects()
    return root, workspace, symbols


def test_real_graph_expands_lexical_seeds_on_mini_auth(stores, tmp_path: Path) -> None:
    root, workspace, symbols = _indexed(tmp_path, stores)
    docs = build_documents(root, symbols)
    by_id = {doc.symbol.id: doc.symbol for doc in docs}
    graph = RepoGraph.from_stores(stores, workspace.repository_id)
    query = build_query(
        "Session.login password expires", repo_files=(s.path for s in symbols), repo_root=root
    )
    seeds = BM25Channel(docs).search(query)
    channel = StructuralChannel(graph, docs)
    found = channel.search(seeds)
    assert found, "真实调用图上应当至少扩展出一个候选"
    assert [c.score for c in found] == sorted((c.score for c in found), reverse=True)

    seed_ids = {c.symbol_id for c in seeds[: channel.seed_limit] if c.score > 0}
    assert seed_ids, "BM25 在 mini_auth 上应当给出正分种子"

    # 每个候选都在某个正分种子的两步邻域内。
    reachable: set[str] = set()
    level = {by_id[sid].qualified_name for sid in seed_ids}
    for _ in range(channel.max_depth):
        following: set[str] = set()
        for qname in level:
            neighbors = {n.symbol.id for n in graph.adjacent(qname)}
            reachable |= neighbors
            following |= {by_id[nid].qualified_name for nid in neighbors if nid in by_id}
        level = following
    assert {c.symbol_id for c in found} <= reachable

    assert channel.search(seeds) == found
    assert StructuralChannel(graph, list(reversed(docs))).search(seeds) == found


def test_service_structural_preserves_result_contract(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\ndef delta(): pass\n",
        encoding="utf-8",
    )
    symbols = [
        document(sid, "", "a.py", line).symbol
        for line, sid in enumerate(("alpha", "beta", "gamma", "delta"), start=1)
    ]
    alpha = next(symbol for symbol in symbols if symbol.id == "alpha")
    beta = next(symbol for symbol in symbols if symbol.id == "beta")
    graph = FakeGraph([(alpha, beta, "CALL", "high", 1)])

    # 默认种子来自词法通道：BM25 的分不是整数，这里只断言扩展关系。
    default = retrieve_symbols(tmp_path, symbols, "alpha", channel="structural", graph=graph)
    assert [c.symbol_id for c in default.symbols] == ["beta"]
    assert 0 < default.symbols[0].score < 1
    assert default.enabled_channels == ["structural"]
    assert default.fusion_method.value == "none"
    assert [c.symbol_id for c in default.per_channel["structural"]] == ["beta"]
    assert default.files == [] and default.channel_weights == {}
    assert RetrievalResult.model_validate_json(default.model_dump_json()) == default

    # 显式种子绕过词法通道，衰减分精确可复算。
    explicit = retrieve_symbols(
        tmp_path,
        symbols,
        "alpha",
        channel="structural",
        graph=graph,
        seeds=[seed("alpha", 1.0)],
    )
    assert [(c.symbol_id, c.score) for c in explicit.symbols] == [("beta", 0.5)]
    assert (
        retrieve_symbols(
            tmp_path,
            symbols,
            "alpha",
            channel="structural",
            graph=graph,
            seeds=[seed("alpha", 1.0)],
            limit=1,
        ).symbols
        == explicit.symbols
    )


def test_service_structural_requires_a_graph(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha(): pass\n", encoding="utf-8")
    alpha = document("alpha", "", "a.py", 1).symbol
    with pytest.raises(ValueError, match="graph"):
        retrieve_symbols(tmp_path, [alpha], "alpha", channel="structural")
    with pytest.raises(ValueError, match="Unknown retrieval channel"):
        retrieve_symbols(tmp_path, [alpha], "alpha", channel="fusion")


def test_service_existing_channels_keep_their_contracts(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\ndef delta(): pass\n",
        encoding="utf-8",
    )
    symbols = [
        document(sid, "", "a.py", line).symbol
        for line, sid in enumerate(("alpha", "beta", "gamma", "delta"), start=1)
    ]
    for channel in ("bm25", "ripgrep"):
        result = retrieve_symbols(tmp_path, symbols, "alpha", channel=channel)
        assert result.enabled_channels == [channel]
        assert list(result.per_channel) == [channel]
        assert [c.symbol_id for c in result.symbols] == ["alpha"]
    with pytest.raises(ValueError, match="embedding_provider"):
        retrieve_symbols(tmp_path, symbols, "alpha", channel="semantic")
