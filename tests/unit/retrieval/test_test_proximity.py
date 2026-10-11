"""V-12: walk TEST edges backwards from tests named in the issue to the code under test.

Scores come straight from R-12's three confidence tiers (high 1.0 / medium 0.6 /
low 0.3), so every expectation is an exact number; several tests contributing to one
symbol are summed in test-ID order so the same input always yields the same float.
"""

import shutil
from pathlib import Path

import pytest

from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.documents import RetrievalError, build_documents
from codepilot.retrieval.query import build_query
from codepilot.retrieval.service import retrieve_symbols
from codepilot.retrieval.test_proximity import TestProximityChannel
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.retrieval import RetrievalQuery, RetrievalResult
from support.retrieval import FakeGraph, document

FIXTURE_REPOS = Path(__file__).resolve().parents[2] / "fixtures" / "repos"

TESTS = "tests/test_tokens.py"


def _corpus(*, test_confidence="high"):
    test = document("t1", "test body", TESTS, 5, kind="test_function")
    production = document("p1", "issue_token body", "src/miniauth/tokens.py", 9)
    other = document("p2", "verify_token body", "src/miniauth/tokens.py", 20)
    edges = [(test.symbol, production.symbol, "TEST", test_confidence, 5)]
    return [test, production, other], edges


def _query(**kwargs):
    return RetrievalQuery(raw_issue=kwargs.pop("raw_issue", "issue text"), **kwargs)


def test_a_named_test_lifts_the_symbol_it_calls():
    docs, edges = _corpus()
    channel = TestProximityChannel(FakeGraph(edges), docs)
    found = channel.search(_query(test_names=["t1"]))
    assert [c.symbol_id for c in found] == ["p1"]
    assert found[0].score == 1.0
    assert found[0].channel_scores[0].channel == "test"
    assert found[0].channel_scores[0].normalized == 1.0


def test_confidence_tiers_are_the_r12_weights():
    for confidence, expected in (("high", 1.0), ("medium", 0.6), ("low", 0.3)):
        docs, edges = _corpus(test_confidence=confidence)
        found = TestProximityChannel(FakeGraph(edges), docs).search(_query(test_names=["t1"]))
        assert found[0].score == expected


def test_dotted_and_bare_test_name_forms_both_match():
    test = document(
        "m1",
        "body",
        TESTS,
        7,
        qualified_name="tests.test_tokens.TestTokens.test_login",
        kind="test_method",
    )
    production = document("p1", "body", "app.py", 3)
    docs, edges = [test, production], [(test.symbol, production.symbol, "TEST", "high", 7)]
    channel = TestProximityChannel(FakeGraph(edges), docs)
    for token in ("test_login", "TestTokens.test_login", "tests.test_tokens.TestTokens.test_login"):
        assert [c.symbol_id for c in channel.search(_query(test_names=[token]))] == ["p1"], token
    assert channel.search(_query(test_names=["login"])) == []
    assert channel.search(_query(test_names=["TestTokens"])) == []


def test_traceback_test_frames_count_as_evidence():
    docs, edges = _corpus()
    channel = TestProximityChannel(FakeGraph(edges), docs)
    frame = TraceFrame(file=TESTS, line=6, function="t1", in_repo=True)
    assert [c.symbol_id for c in channel.search(_query(parsed_frames=[frame]))] == ["p1"]
    # Third-party frames and frames inside production files are not this channel's evidence.
    assert (
        channel.search(
            _query(parsed_frames=[TraceFrame(file=TESTS, line=6, function="t1", in_repo=False)])
        )
        == []
    )
    assert (
        channel.search(
            _query(parsed_frames=[TraceFrame(file="app.py", line=1, function="t1", in_repo=True)])
        )
        == []
    )
    assert (
        channel.search(
            _query(parsed_frames=[TraceFrame(file=TESTS, line=6, function="other", in_repo=True)])
        )
        == []
    )


def test_every_matching_test_contributes():
    first = document("t1", "body", TESTS, 5, kind="test_function")
    second = document("t2", "body", TESTS, 9, kind="test_function")
    shared = document("p1", "body", "app.py", 3)
    single = document("p2", "body", "app.py", 8)
    edges = [
        (first.symbol, shared.symbol, "TEST", "high", 5),
        (second.symbol, shared.symbol, "TEST", "medium", 9),
        (first.symbol, single.symbol, "TEST", "high", 5),
    ]
    found = TestProximityChannel(FakeGraph(edges), [first, second, shared, single]).search(
        _query(test_names=["t1", "t2"])
    )
    assert [(c.symbol_id, c.score) for c in found] == [("p1", 1.6), ("p2", 1.0)]


def test_strongest_edge_from_one_test_is_the_one_counted():
    test = document("t1", "body", TESTS, 5, kind="test_function")
    target = document("p1", "body", "app.py", 3)
    edges = [
        (test.symbol, target.symbol, "TEST", "low", 5),
        (test.symbol, target.symbol, "TEST", "high", 8),
        (test.symbol, target.symbol, "TEST", "medium", 2),
    ]
    found = TestProximityChannel(FakeGraph(edges), [test, target]).search(_query(test_names=["t1"]))
    assert found[0].score == 1.0


def test_only_test_symbols_are_evidence_and_only_production_is_reported():
    production_named_test = document("legacy", "body", "app.py", 1)  # kind=function
    test = document("t1", "body", TESTS, 5, kind="test_function")
    other_test = document("t2", "body", TESTS, 9, kind="test_method")
    outside = document("gone", "body", "app.py", 4)
    edges = [
        (production_named_test.symbol, test.symbol, "TEST", "high", 1),
        (test.symbol, other_test.symbol, "TEST", "high", 5),
        (test.symbol, outside.symbol, "TEST", "high", 6),
        (test.symbol, production_named_test.symbol, "TEST", "medium", 7),
    ]
    docs = [production_named_test, test, other_test]
    channel = TestProximityChannel(FakeGraph(edges), docs)
    # A symbol whose name looks like a test but is not a test kind gives no evidence.
    assert channel.search(_query(test_names=["legacy"])) == []
    found = channel.search(_query(test_names=["t1"]))
    # Only the production side is reported: t2 is a test, gone is outside the candidate
    # set, and an incoming edge is not a reverse lookup.
    assert [(c.symbol_id, c.score) for c in found] == [("legacy", 0.6)]


def test_incoming_test_edges_are_not_followed():
    caller = document("p0", "body", "app.py", 2)
    docs, edges = _corpus()
    edges.append((caller.symbol, docs[1].symbol, "TEST", "high", 2))
    channel = TestProximityChannel(FakeGraph(edges), [caller, *docs])
    assert [(c.symbol_id, c.score) for c in channel.search(_query(test_names=["t1"]))] == [
        ("p1", 1.0)
    ]
    assert channel.search(_query(test_names=["p0"])) == []


def test_query_without_test_evidence_returns_empty_not_an_error():
    docs, edges = _corpus()
    channel = TestProximityChannel(FakeGraph(edges), docs)
    assert channel.search(_query()) == []
    assert channel.search(_query(test_names=[])) == []
    assert channel.search(_query(test_names=["nope"])) == []
    assert TestProximityChannel(FakeGraph(), docs).search(_query(test_names=["t1"])) == []


def test_top_k_and_normalization_use_the_full_candidate_set():
    test = document("t1", "body", TESTS, 5, kind="test_function")
    targets = [document(f"p{index}", "body", "app.py", index) for index in range(1, 4)]
    edges = [
        (test.symbol, target.symbol, "TEST", ("high", "medium", "low")[index], index + 1)
        for index, target in enumerate(targets)
    ]
    channel = TestProximityChannel(FakeGraph(edges), [test, *targets])
    full = channel.search(_query(test_names=["t1"]))
    assert [(c.symbol_id, c.score) for c in full] == [("p1", 1.0), ("p2", 0.6), ("p3", 0.3)]
    assert [c.channel_scores[0].normalized for c in full] == pytest.approx([1.0, 0.3 / 0.7, 0.0])
    head = channel.search(_query(test_names=["t1"]), limit=2)
    assert head == full[:2]


def test_results_are_independent_of_document_and_edge_order():
    tests = [document(f"t{n}", "body", TESTS, n, kind="test_function") for n in (1, 2, 3)]
    targets = [document(f"p{n}", "body", "app.py", n) for n in (1, 2)]
    evidence = [
        (tests[0], targets[0], "medium"),
        (tests[1], targets[0], "low"),
        (tests[2], targets[1], "high"),
        (tests[0], targets[1], "medium"),
    ]
    edges = [
        (test.symbol, target.symbol, "TEST", confidence, index)
        for index, (test, target, confidence) in enumerate(evidence)
    ]
    docs = [*tests, *targets]
    query = _query(test_names=["t1", "t2", "t3"])
    forward = TestProximityChannel(FakeGraph(edges), docs).search(query)
    backward = TestProximityChannel(FakeGraph(list(reversed(edges))), list(reversed(docs))).search(
        query
    )
    assert forward == backward
    # p1 = 0.6 + 0.3 and p2 = 1.0 + 0.6, each summed in test-ID order.
    assert [c.symbol_id for c in forward] == ["p2", "p1"]
    assert [c.score for c in forward] == pytest.approx([1.6, 0.9])


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit(limit):
    docs, edges = _corpus()
    with pytest.raises(ValueError):
        TestProximityChannel(FakeGraph(edges), docs).search(_query(test_names=["t1"]), limit=limit)


def test_invalid_configuration_and_documents():
    docs, edges = _corpus()
    with pytest.raises(ValueError):
        TestProximityChannel(FakeGraph(edges), docs, kinds=())
    with pytest.raises(RetrievalError, match="unique nonempty"):
        TestProximityChannel(FakeGraph(edges), [*docs, document("t1", "dup", TESTS)])
    with pytest.raises(RetrievalError, match="unique nonempty"):
        TestProximityChannel(FakeGraph(edges), [*docs, document("", "x", TESTS)])


def test_service_test_channel_contract(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha(): pass\ndef beta(): pass\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        "def helper(): pass\n\ndef test_alpha(): pass\n", encoding="utf-8"
    )
    alpha = document("alpha", "", "a.py", 1)
    beta = document("beta", "", "a.py", 2)
    probe = document("test_alpha", "", "tests/test_a.py", 3, kind="test_function")
    graph = FakeGraph([(probe.symbol, alpha.symbol, "TEST", "high", 3)])

    result = retrieve_symbols(
        tmp_path,
        [alpha.symbol, beta.symbol, probe.symbol],
        "test_alpha fails",
        channel="test",
        graph=graph,
    )
    assert [c.symbol_id for c in result.symbols] == ["alpha"]
    assert result.symbols[0].score == 1.0
    assert result.enabled_channels == ["test"]
    assert result.fusion_method.value == "none"
    assert list(result.per_channel) == ["test"]
    assert RetrievalResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValueError, match="graph"):
        retrieve_symbols(tmp_path, [alpha.symbol], "test_alpha", channel="test")


def _indexed(tmp_path: Path, stores):
    root = tmp_path / "mini_auth"
    shutil.copytree(FIXTURE_REPOS / "mini_auth", root)
    workspace = Workspace(
        path=root,
        source=normalize_source(str(root)),
        base_commit="0" * 40,
        repository_id="repo_test_proximity",
    )
    Indexer().index(stores, workspace)
    symbols = RepositoryIndex(stores, workspace.repository_id).symbol_objects()
    return root, workspace, symbols


@pytest.mark.parametrize(
    "issue",
    [
        "test_issue_returns_three_parts fails: the token has only two parts",
        (
            "The token test is red.\n\n"
            '  File "tests/test_tokens.py", line 5, in test_issue_returns_three_parts\n'
            "    token = issue_token('demo')\n"
        ),
    ],
)
def test_real_test_edges_localize_the_symbol_under_test_on_mini_auth(
    stores, tmp_path: Path, issue: str
) -> None:
    root, workspace, symbols = _indexed(tmp_path, stores)
    docs = build_documents(root, symbols)
    graph = RepoGraph.from_stores(stores, workspace.repository_id)
    query = build_query(issue, repo_files=(s.path for s in symbols), repo_root=root)
    channel = TestProximityChannel(graph, docs)
    found = channel.search(query)
    assert found, "real TEST edges should point back at the symbol under test"
    assert found[0].qualified_name == "miniauth.tokens.issue_token"
    assert query.test_names or query.parsed_frames
    assert channel.search(query) == found
    assert TestProximityChannel(graph, list(reversed(docs))).search(query) == found
