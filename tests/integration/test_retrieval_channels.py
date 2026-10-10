"""V-11~V-13 closeout: the three second-stage channels, independently scored on one real repo.

Real git, real index, real SQLite on purpose. The channel unit tests prove the
algorithms against an in-memory adjacency table; this file proves the wiring
(GraphSource / GitHistory / RepositoryIndex against the service contract) and that
commits after the base cannot leak into the ranking through the history channel.
"""

from pathlib import Path

import pytest

from codepilot.repo.git_history import GitHistory
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.documents import SymbolDocument, build_documents
from codepilot.retrieval.history import HistoryChannel
from codepilot.retrieval.lexical import BM25Channel
from codepilot.retrieval.query import build_query
from codepilot.retrieval.service import retrieve_symbols
from codepilot.retrieval.structural import StructuralChannel
from codepilot.retrieval.test_proximity import TestProximityChannel
from support.gitfixture import init_repo

ISSUE = (
    "Expired tokens keep working.\n\n"
    '  File "tests/test_tokens.py", line 5, in test_issue_returns_three_parts\n'
    "    token = issue_token('demo')\n"
    "ValueError: invalid token\n"
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "mini_auth"


@pytest.fixture
def repo(tmp_path: Path):
    """mini_auth as a real git repo; one extra pre-base tokens.py edit gives churn spread."""
    files = {
        path.relative_to(FIXTURE).as_posix(): path.read_text(encoding="utf-8")
        for path in FIXTURE.rglob("*")
        if path.is_file() and path.suffix == ".py"
    }
    root = tmp_path / "mini_auth"
    fixture_repo = init_repo(root, files)
    tokens = root / "src" / "miniauth" / "tokens.py"
    tokens.write_text(tokens.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    base = fixture_repo.commit("touch tokens")
    return root, fixture_repo, base


@pytest.fixture
def indexed(stores, repo):
    root, _, base = repo
    workspace = Workspace(
        path=root,
        source=normalize_source(str(root)),
        base_commit=base,
        repository_id="repo_channels",
    )
    Indexer().index(stores, workspace)
    symbols = RepositoryIndex(stores, workspace.repository_id).symbol_objects()
    return root, workspace, symbols


def test_each_channel_reads_only_its_declared_inputs(stores, indexed) -> None:
    root, workspace, symbols = indexed
    documents = build_documents(root, symbols)
    graph = RepoGraph.from_stores(stores, workspace.repository_id)
    query = build_query(ISSUE, repo_files=(s.path for s in symbols), repo_root=root)
    churn = GitHistory(root, workspace.base_commit).file_churn()

    structural = retrieve_symbols(root, symbols, ISSUE, channel="structural", graph=graph, limit=10)
    proximity = retrieve_symbols(root, symbols, ISSUE, channel="test", graph=graph, limit=10)
    history = retrieve_symbols(root, symbols, ISSUE, channel="history", churn=churn, limit=10)

    # Feeding each channel only its declared inputs must reproduce the service result.
    assert (
        structural.symbols
        == StructuralChannel(graph, documents).search(BM25Channel(documents).search(query))[:10]
    )
    assert proximity.symbols == TestProximityChannel(graph, documents).search(query)[:10]
    assert history.symbols == HistoryChannel(documents, churn).search()[:10]

    for name, result in (
        ("structural", structural),
        ("test", proximity),
        ("history", history),
    ):
        assert result.enabled_channels == [name]
        assert list(result.per_channel) == [name]
        assert result.fusion_method.value == "none"
        assert result.symbols, f"the {name} channel should rank candidates on mini_auth"
        assert all(
            score.channel == name for c in result.per_channel[name] for score in c.channel_scores
        )


def test_history_channel_is_query_independent(stores, indexed) -> None:
    root, workspace, symbols = indexed
    churn = GitHistory(root, workspace.base_commit).file_churn()
    first = retrieve_symbols(root, symbols, ISSUE, channel="history", churn=churn)
    second = retrieve_symbols(
        root, symbols, "totally unrelated wording about plugins", channel="history", churn=churn
    )
    assert first.symbols == second.symbols
    assert first.query.raw_issue != second.query.raw_issue


def test_results_are_identical_across_runs_and_input_order(stores, indexed) -> None:
    root, workspace, symbols = indexed
    documents = build_documents(root, symbols)
    graph = RepoGraph.from_stores(stores, workspace.repository_id)
    query = build_query(ISSUE, repo_files=(s.path for s in symbols), repo_root=root)
    churn = GitHistory(root, workspace.base_commit).file_churn()
    seeds = BM25Channel(documents).search(query)

    cases = {
        "structural": {"graph": graph, "seeds": seeds},
        "test": {"graph": graph},
        "history": {"churn": churn},
    }
    for label, kwargs in cases.items():
        first = retrieve_symbols(root, symbols, ISSUE, channel=label, **kwargs)
        again = retrieve_symbols(root, symbols, ISSUE, channel=label, **kwargs)
        assert first.model_dump_json() == again.model_dump_json(), label
        reordered = retrieve_symbols(root, list(reversed(symbols)), ISSUE, channel=label, **kwargs)
        assert first.model_dump_json() == reordered.model_dump_json(), label


def test_history_channel_cannot_see_commits_after_the_base(stores, repo, indexed) -> None:
    _, fixture_repo, base = repo
    root, _, symbols = indexed
    documents = build_documents(root, symbols)
    before = HistoryChannel(documents, GitHistory(root, base).file_churn()).search()

    # Add and commit a file after the base: neither the index nor the channel input sees it.
    (root / "src" / "miniauth" / "post_base.py").write_text(
        "def added_after_base():\n    return 1\n", encoding="utf-8"
    )
    fixture_repo.commit("post-base addition")
    after_churn = GitHistory(root, base).file_churn()
    assert "src/miniauth/post_base.py" not in after_churn
    after = HistoryChannel(documents, after_churn).search()
    assert after == before
    assert all(c.file != "src/miniauth/post_base.py" for c in after)


def test_the_base_tree_still_yields_test_symbols(indexed) -> None:
    root, _, symbols = indexed
    documents = build_documents(root, symbols)
    assert all(isinstance(document, SymbolDocument) for document in documents)
    assert any(document.symbol.kind == "test_function" for document in documents), (
        "mini_auth's test files should yield test_function symbols for V-12 to follow"
    )
