"""V-11~V-13 收口：三个二段式通道在同一份真实仓库上独立跑、独立评分、可复现。

这里刻意用真 git + 真索引 + 真 SQLite：通道单测用内存邻域表证明算法，这一支证明
接线是对的（GraphSource / GitHistory / RepositoryIndex 与 service 的契约），
并且 base 之后的提交不会通过 history 通道渗进排序。
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
    """一份真 git 化的 mini_auth；base 之前多一次 tokens.py 修改，制造 churn 差异。"""
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

    # 与直接构造通道、只喂它声明的输入，结果必须逐字段一致。
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
        assert result.symbols, f"{name} 通道在 mini_auth 上应当给出候选"
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

    # base 之后新增文件并提交：索引与通道输入都不应看到它。
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
        "mini_auth 的测试文件应当给出 test_function，供 V-12 反推"
    )
