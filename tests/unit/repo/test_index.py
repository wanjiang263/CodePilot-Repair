"""R-15 / R-16：索引持久化与增量重建。

持久化的正确性用一条**等价性**断言钉住：增量重建后的数据库必须与"同一
仓库状态下的一次全新全量索引"逐字段一致。这条断言同时覆盖 symbol 重建、
边的删除/重建与悬挂清理——任何一边漏掉，两边就会不同。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.models import score_to_confidence
from codepilot.repo.parser import ParsedFile, TreeSitterParser
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.models import SymbolRow
from codepilot.storage.repository import Stores
from support.indexdump import dump_index as _dump

FIXTURE_REPOS = Path(__file__).resolve().parents[2] / "fixtures" / "repos"


class CountingParser:
    """记录每次 ``parse`` 调用的路径。R-16 的"只重新解析变化文件"靠它断言。"""

    name = "tree-sitter"

    def __init__(self) -> None:
        self._inner = TreeSitterParser()
        self.paths: list[str] = []

    def parse(self, source: bytes, *, path: str = "") -> ParsedFile:
        self.paths.append(path)
        return self._inner.parse(source, path=path)


def _ws(path: Path, repository_id: str = "repo_test_index") -> Workspace:
    return Workspace(
        path=path,
        source=normalize_source(str(path)),
        base_commit="0" * 40,  # 单测不涉及 git 历史；集成测试用真 SHA
        repository_id=repository_id,
    )


def _copy_fixture(tmp_path: Path, name: str = "mini_auth") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE_REPOS / name, target)
    return target


@pytest.fixture
def fresh_stores() -> Stores:
    """第二个内存库：给"全新全量索引"做对照。"""
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    session = factory()
    stores = Stores(session)
    yield stores
    session.rollback()
    session.close()
    engine.dispose()


# ---------------------------------------------------------------- R-15 持久化


def test_full_index_persists_files_symbols_edges_tests(stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    stats = Indexer().index(stores, _ws(root))
    index = RepositoryIndex(stores, stats.repository_id)

    assert index.exists()
    files = index.files()
    assert len(files) == 14  # 12 个 .py + README.md + pyproject.toml
    by_path = {f.path: f for f in files}
    assert by_path["README.md"].module is None
    assert by_path["src/miniauth/session.py"].module == "miniauth.session"
    assert by_path["src/miniauth/__init__.py"].module == "miniauth"
    assert by_path["tests/test_session.py"].is_test
    assert not by_path["src/miniauth/session.py"].is_test

    kinds = stats.symbols_by_kind
    assert kinds["module"] == 12
    assert kinds["class"] >= 6  # User/AuthBackend/MemoryBackend/Session/Admin/Audited
    assert kinds["test_function"] + kinds["test_method"] > 0
    assert stats.tests == kinds["test_function"] + kinds["test_method"]

    edge_kinds = {kind for (kind, _), count in stats.edges_by_kind_confidence.items() if count}
    assert edge_kinds == {"CALL", "IMPORT", "INHERIT", "REFERENCE", "TEST"}
    confidences = {conf for (_, conf), count in stats.edges_by_kind_confidence.items() if count}
    assert confidences <= {"high", "medium", "low"}

    tests = index.tests()
    assert tests and all(t.kind in {"test_function", "test_method"} for t in tests)


def test_persisted_symbol_fields_round_trip(stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    stats = Indexer().index(stores, _ws(root))
    index = RepositoryIndex(stores, stats.repository_id)

    rows = index.symbols()
    by_qname: dict[str, SymbolRow] = {row.qualified_name: row for row in rows}
    login = by_qname["miniauth.session.Session.login"]
    assert login.kind == "method"
    assert login.signature == "def login(self, password: str) -> str | None"
    assert login.docstring == "登录成功返回令牌。"
    assert login.start_line < login.end_line
    parent = by_qname["miniauth.session.Session"]
    assert login.parent_id == parent.id

    build = by_qname["miniauth.app.build_session"]
    assert build.decorators == ["audited", "retry(\n    times=5,\n)"]


def test_edges_persisted_with_dedup_and_line(stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    stats = Indexer().index(stores, _ws(root))
    edges = RepositoryIndex(stores, stats.repository_id).edges()
    assert edges
    keys = {(e.source_id, e.target_id, e.target_name, e.kind) for e in edges}
    assert len(keys) == len(edges)  # uq_edge_identity 无重复
    assert all(e.line >= 1 for e in edges)
    # 每条边的 source / target 都能在 symbols 表里找到（target_id 非空时）。
    symbol_ids = {s.id for s in RepositoryIndex(stores, stats.repository_id).symbols()}
    assert all(e.source_id in symbol_ids for e in edges)
    assert all(e.target_id is None or e.target_id in symbol_ids for e in edges)


def test_reindex_is_idempotent(stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    workspace = _ws(root)

    first = indexer.index(stores, workspace)
    assert not first.incremental
    before = _dump(stores, workspace.repository_id)
    parser.paths.clear()

    second = indexer.index(stores, workspace)
    after = _dump(stores, workspace.repository_id)

    assert parser.paths == []  # 未变化文件不得重复解析
    assert second.incremental
    assert second.changed_paths == ()
    assert len(second.reused_paths) == 14
    assert second.files == 14
    assert before == after  # 重复索引不得产生重复记录


# ---------------------------------------------------------------- R-16 增量重建


def test_incremental_reindex_parses_only_the_changed_file(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    root = _copy_fixture(tmp_path)
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    workspace = _ws(root)
    indexer.index(stores, workspace)
    parser.paths.clear()

    tokens = root / "src" / "miniauth" / "tokens.py"
    tokens.write_text(
        tokens.read_text(encoding="utf-8").replace("ttl: int = 3600", "ttl: int = 7200"),
        encoding="utf-8",
        newline="\n",
    )
    stats = indexer.index(stores, workspace)

    assert parser.paths == ["src/miniauth/tokens.py"]
    assert stats.changed_paths == ("src/miniauth/tokens.py",)
    assert len(stats.reused_paths) == 13

    # 等价性：与同一状态下的全新全量索引逐字段一致。
    fresh = Indexer().index(fresh_stores, workspace)
    assert _dump(stores, workspace.repository_id) == _dump(fresh_stores, fresh.repository_id)


def test_incremental_reindex_handles_symbol_rename_and_dangling_edges(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    root = _copy_fixture(tmp_path)
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    workspace = _ws(root)
    indexer.index(stores, workspace)
    parser.paths.clear()

    tokens = root / "src" / "miniauth" / "tokens.py"
    source = tokens.read_text(encoding="utf-8")
    tokens.write_text(source.replace("issue_token", "mint_token"), encoding="utf-8", newline="\n")
    indexer.index(stores, workspace)

    assert parser.paths == ["src/miniauth/tokens.py"]
    index = RepositoryIndex(stores, workspace.repository_id)
    qnames = {s.qualified_name for s in index.symbols()}
    assert "miniauth.tokens.mint_token" in qnames
    assert "miniauth.tokens.issue_token" not in qnames

    edges = index.edges()
    symbol_ids = {s.id for s in index.symbols()}
    # 指向已删除 symbol 的边（session.py / __init__.py 对 issue_token 的引用）
    # 必须被清理——这些源文件本身没有变化，靠的是悬挂删除而不是重建。
    assert all(e.target_id is None or e.target_id in symbol_ids for e in edges)
    assert not any(e.target_name.endswith("issue_token") and e.target_id is not None for e in edges)

    fresh = Indexer().index(fresh_stores, workspace)
    assert _dump(stores, workspace.repository_id) == _dump(fresh_stores, fresh.repository_id)


def test_incremental_reindex_removes_deleted_files(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    root = _copy_fixture(tmp_path)
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    workspace = _ws(root)
    indexer.index(stores, workspace)
    parser.paths.clear()

    (root / "tests" / "test_tokens.py").unlink()
    stats = indexer.index(stores, workspace)

    assert parser.paths == []
    assert stats.removed_paths == ("tests/test_tokens.py",)
    index = RepositoryIndex(stores, workspace.repository_id)
    assert all(f.path != "tests/test_tokens.py" for f in index.files())
    assert all(not s.qualified_name.startswith("tests.test_tokens") for s in index.symbols())
    assert all(e.source_id in {s.id for s in index.symbols()} for e in index.edges())

    fresh = Indexer().index(fresh_stores, workspace)
    assert _dump(stores, workspace.repository_id) == _dump(fresh_stores, fresh.repository_id)


def test_incremental_reindex_new_file(stores: Stores, fresh_stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    workspace = _ws(root)
    indexer.index(stores, workspace)
    parser.paths.clear()

    extra = root / "src" / "miniauth" / "extra.py"
    extra.write_text("def helper() -> int:\n    return 1\n", encoding="utf-8", newline="\n")
    stats = indexer.index(stores, workspace)

    assert parser.paths == ["src/miniauth/extra.py"]
    assert stats.changed_paths == ("src/miniauth/extra.py",)
    index = RepositoryIndex(stores, workspace.repository_id)
    assert "miniauth.extra.helper" in {s.qualified_name for s in index.symbols()}

    fresh = Indexer().index(fresh_stores, workspace)
    assert _dump(stores, workspace.repository_id) == _dump(fresh_stores, fresh.repository_id)


def test_parse_error_file_does_not_abort_indexing(stores: Stores, tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path, "mini_calc")
    stats = Indexer().index(stores, _ws(root, "repo_test_calc"))
    index = RepositoryIndex(stores, stats.repository_id)

    assert stats.parse_errors
    assert any("broken.py" in error for error in stats.parse_errors)
    by_path = {f.path: f for f in index.files()}
    assert by_path["calc/broken.py"].parse_error is not None
    # 其余文件照常索引；语法错误文件仍贡献可识别的定义（R-05 语义）。
    assert "calc.core" in {f.module for f in index.files()}
    assert index.symbols()


# ------------------------------------------------- 遗留边界 #2：medium TEST 关联

_TOKENS_V1 = "def issue_token() -> str:\n    return 'x'\n"
_TOKENS_V2 = (
    "def issue_token() -> str:\n    return 'x'\n\n\ndef mint_token() -> str:\n    return 'y'\n"
)
_TEST_TOKENS = "from lib.tokens import issue_token\n\n\ndef test_mint_token() -> None:\n    assert issue_token()\n"


def _write(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def _test_edge_confidence(stores: Stores, repository_id: str, test_qname: str, target_qname: str):
    """取 test→target 的 TEST 边置信度（字符串三档），无边返回 None。"""
    index = RepositoryIndex(stores, repository_id)
    ids = {s.qualified_name: s.id for s in index.symbols()}
    if test_qname not in ids or target_qname not in ids:
        return None
    for edge in index.edges(kind="TEST"):
        if edge.source_id == ids[test_qname] and edge.target_id == ids[target_qname]:
            return score_to_confidence(edge.confidence)
    return None


def test_unchanged_test_file_gets_low_but_not_medium_link_to_new_symbol(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    """钉住遗留边界 #2（index.py 模块 docstring）：

    生产文件新增一个 symbol、而测试文件**未变化**时，增量索引只会补上 low
    （文件名约定）关联，补不上 medium（import + 测试名包含 symbol 名）——
    medium 需要该测试文件自己的 import 绑定，而增量不重新解析未变化文件。
    全量索引会给 medium；等测试文件自己变化后，增量也会自愈到 medium。
    """
    root = tmp_path / "proj"
    _write(root, "src/lib/__init__.py", "")
    _write(root, "src/lib/tokens.py", _TOKENS_V1)
    _write(root, "tests/test_tokens.py", _TEST_TOKENS)
    workspace = _ws(root, "repo_medium_boundary")
    indexer = Indexer()
    indexer.index(stores, workspace)

    test_qname = "tests.test_tokens.test_mint_token"
    mint_qname = "lib.tokens.mint_token"

    # 生产文件新增 mint_token；测试文件不动。
    _write(root, "src/lib/tokens.py", _TOKENS_V2)
    stats = indexer.index(stores, workspace)
    assert stats.parsed_paths == ("src/lib/tokens.py",)

    # 增量：只补上 low（文件名约定 tokens == test_tokens 去前缀），medium 缺失。
    assert _test_edge_confidence(stores, workspace.repository_id, test_qname, mint_qname) == "low"

    # 同一状态的全量索引：medium（import lib.tokens + "mint_token" ⊂ "test_mint_token"）。
    Indexer().index(fresh_stores, workspace)
    assert (
        _test_edge_confidence(fresh_stores, workspace.repository_id, test_qname, mint_qname)
        == "medium"
    )

    # 测试文件自己变化后，增量重解析它 → medium 自愈。
    _write(root, "tests/test_tokens.py", "# touched\n" + _TEST_TOKENS)
    stats = indexer.index(stores, workspace)
    assert "tests/test_tokens.py" in stats.parsed_paths
    assert (
        _test_edge_confidence(stores, workspace.repository_id, test_qname, mint_qname) == "medium"
    )
