"""V-11~V-13 acceptance smoke: real git, real index, real RepoGraph and GitHistory.

No test doubles. Only the issue text is hand-written, so this exercises the wiring
(service → channels → R-17 graph / R-13 history) end to end and keeps the resulting
channel rankings as auditable artifacts.
"""

import json
import sys
import tempfile
from pathlib import Path

from codepilot.repo.graph import RepoGraph
from codepilot.repo.git_history import GitHistory
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.documents import build_documents
from codepilot.retrieval.lexical import BM25Channel
from codepilot.retrieval.query import build_query
from codepilot.retrieval.service import retrieve_symbols
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import Stores

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "tests"))
from support.gitfixture import init_repo  # noqa: E402

evidence = Path(__file__).parent
fixture = root / "tests/fixtures/repos/mini_auth"

STRUCTURAL_ISSUE = "issue_token returns a malformed token"
PROXIMITY_ISSUE = "test_issue_returns_three_parts fails: the token has only two parts"
HISTORY_ISSUE = "token expiry is wrong"
POST_BASE_FILE = "src/miniauth/post_base.py"

files = {
    path.relative_to(fixture).as_posix(): path.read_text(encoding="utf-8")
    for path in fixture.rglob("*")
    if path.is_file() and path.suffix in {".py", ".toml", ".md"}
}


def run(repo_path: Path, symbols, issue: str, **kwargs):
    return retrieve_symbols(repo_path, symbols, issue, limit=8, **kwargs)


engine = create_engine_from_url("sqlite://")
init_db(engine)
try:
    with tempfile.TemporaryDirectory(prefix="codepilot-structural-") as directory:
        repo = init_repo(Path(directory) / "repo", files)
        tokens = repo.path / "src/miniauth/tokens.py"
        tokens.write_text(tokens.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        base = repo.commit("touch tokens")

        with create_session_factory(engine)() as session:
            stores = Stores(session)
            workspace = Workspace(
                path=repo.path,
                source=normalize_source(str(repo.path)),
                base_commit=base,
                repository_id="structural_smoke",
            )
            stats = Indexer().index(stores, workspace)
            symbols = RepositoryIndex(stores, workspace.repository_id).symbol_objects()
            graph = RepoGraph.from_stores(stores, workspace.repository_id)
            churn = GitHistory(repo.path, base).file_churn()
            query = build_query(
                STRUCTURAL_ISSUE,
                repo_files=(symbol.path for symbol in symbols),
                repo_root=repo.path,
            )
            seeds = BM25Channel(build_documents(repo.path, symbols)).search(query)
            structural = run(repo.path, symbols, STRUCTURAL_ISSUE, channel="structural", graph=graph)
            proximity = run(repo.path, symbols, PROXIMITY_ISSUE, channel="test", graph=graph)
            history = run(repo.path, symbols, HISTORY_ISSUE, channel="history", churn=churn)
            lexical = run(repo.path, symbols, STRUCTURAL_ISSUE, channel="bm25")

            assert seeds and structural.symbols, "词法种子与结构扩展都应当非空"
            positive = {c.symbol_id for c in seeds[:20] if c.score > 0}
            qname_by_id = {symbol.id: symbol.qualified_name for symbol in symbols}
            hops: set[str] = set()
            level = {qname_by_id[sid] for sid in positive}
            for _ in range(2):
                following: set[str] = set()
                for qname in level:
                    for neighbor in graph.adjacent(qname):
                        hops.add(neighbor.symbol.id)
                        following.add(neighbor.symbol.qualified_name)
                level = following
            expanded = {c.symbol_id for c in structural.per_channel["structural"]}
            assert expanded <= hops, "结构候选必须落在种子的两步邻域内"

            assert proximity.symbols[0].qualified_name == "miniauth.tokens.issue_token"
            assert proximity.symbols[0].score == 1.0, "high：测试体内直接调用"

            assert churn["src/miniauth/tokens.py"].modifications == 2
            assert churn["src/miniauth/tokens.py"].last_modified_distance == 0
            assert POST_BASE_FILE not in churn
            assert history.symbols[0].file == "src/miniauth/tokens.py"
            assert len({c.score for c in history.per_channel["history"]}) == 2

            for name, result in (
                ("structural", structural),
                ("test", proximity),
                ("history", history),
            ):
                assert result.enabled_channels == [name]
                assert list(result.per_channel) == [name]
                assert result.fusion_method.value == "none"
                assert result.files == [] and result.channel_weights == {}

            # base 之后新增文件并提交：索引与检索输入都停在 base。
            (repo.path / POST_BASE_FILE).write_text(
                "def added_after_base():\n    return 1\n", encoding="utf-8"
            )
            after = repo.commit("post-base addition")
            assert repo.git("status", "--porcelain") == ""
            post_churn = GitHistory(repo.path, base).file_churn()
            assert post_churn == churn, "base 之后的提交不得改变历史特征"
            assert POST_BASE_FILE not in post_churn
            assert symbols and not any(symbol.path == POST_BASE_FILE for symbol in symbols)

            rerun = run(
                repo.path, list(reversed(symbols)), STRUCTURAL_ISSUE, channel="structural",
                graph=graph,
            )
            assert rerun.model_dump_json() == structural.model_dump_json(), "同输入必须逐字节一致"

            for name, result in (
                ("bm25", lexical),
                ("structural", structural),
                ("test", proximity),
                ("history", history),
            ):
                (evidence / f"channel-{name}.json").write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )

            summary = {
                "status": "PASS",
                "doubles_used": "none (real git repository, real tree-sitter index, real NetworkX graph)",
                "base_commit": base,
                "post_base_commit": after,
                "repository_id": workspace.repository_id,
                "indexed_files": stats.files,
                "indexed_symbols": len(symbols),
                "edges_by_kind_confidence": {
                    f"{kind}/{confidence}": count
                    for (kind, confidence), count in stores.edges.count_by_kind_confidence(
                        workspace.repository_id
                    ).items()
                },
                "files_with_churn": len(churn),
                "structural_seeds": [c.qualified_name for c in seeds[:3]],
                "structural_top": [(c.file, c.qualified_name, c.score) for c in structural.symbols[:5]],
                "test_top": [(c.file, c.qualified_name, c.score) for c in proximity.symbols[:5]],
                "history_top": [(c.file, c.qualified_name, c.score) for c in history.symbols[:5]],
                "lexical_top": [(c.qualified_name, c.score) for c in lexical.symbols[:3]],
                "post_base_file_in_history": POST_BASE_FILE in post_churn,
                "deterministic_rerun_identical": rerun.model_dump_json() == structural.model_dump_json(),
                "workspace_clean_after_run": repo.git("status", "--porcelain") == "",
            }
            (evidence / "structural-smoke.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            print(json.dumps(summary, indent=2))
finally:
    engine.dispose()
