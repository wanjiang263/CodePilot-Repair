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


def test_scan_does_not_evict_every_initial_cache_hit(stores: Stores, tmp_path: Path) -> None:
    root = tmp_path / "large-enough-for-lru"
    root.mkdir()
    for i in range(130):
        (root / f"mod{i:03}.py").write_text("def value():\n    return 1\n", encoding="utf8")
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    indexer.index(stores, _ws(root))
    (root / "mod000.py").write_text("def value():\n    return 2\n", encoding="utf8")
    stats = indexer.index(stores, _ws(root))
    assert len(stats.parsed_paths) == 2
    assert len(stats.parse_reused_paths) == 128
    assert stats.association_mode == "complete_closure_v2"


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


def test_unchanged_test_file_gets_medium_link_to_new_symbol(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    """Phase26: complete association closure must agree with fresh indexing; NOT EXECUTED."""
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

    # 未变化测试的解析树复用，绑定和 TEST 关联仍重建。
    assert (
        _test_edge_confidence(stores, workspace.repository_id, test_qname, mint_qname) == "medium"
    )

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


# ----------------------------------------- 遗留边界 #1：未变化文件的出边不重建


def _import_edges(
    index: RepositoryIndex, source_qname: str, target_name: str
) -> list[tuple[bool, str]]:
    """返回 ``source 模块 → target_name`` 的 IMPORT 边集合，每项 ``(target 是否已解析, confidence)``。"""
    ids = {s.qualified_name: s.id for s in index.symbols()}
    source_id = ids.get(source_qname)
    return [
        (e.target_id is not None, score_to_confidence(e.confidence))
        for e in index.edges(kind="IMPORT")
        if e.source_id == source_id and e.target_name == target_name
    ]


def test_incremental_rebuilds_unchanged_files_outgoing_edges(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    """Phase26: complete association closure must agree with fresh indexing; NOT EXECUTED."""
    root = tmp_path / "p"
    _write(root, "pkg/__init__.py", "")
    # a.py 从一开始就显式引用 pkg.b.newfn，但首次索引时 b 里还没有 newfn → 解析不到。
    _write(root, "pkg/a.py", "from pkg.b import newfn\n\n\ndef use() -> int:\n    return newfn()\n")
    _write(root, "pkg/b.py", "def placeholder() -> int:\n    return 0\n")
    workspace = _ws(root, "repo_bnd_newsym")
    indexer = Indexer()
    indexer.index(stores, workspace)

    # 只改 b.py 新增 newfn；a.py 内容一字不动。
    _write(
        root,
        "pkg/b.py",
        "def placeholder() -> int:\n    return 0\n\n\ndef newfn() -> int:\n    return 2\n",
    )
    stats = indexer.index(stores, workspace)
    assert stats.parsed_paths == ("pkg/b.py",)  # a.py 未被重新解析

    Indexer().index(fresh_stores, workspace)  # 同状态全新全量

    # 增量：复用解析树并重新绑定 a.py 的出边。
    assert _import_edges(
        RepositoryIndex(stores, workspace.repository_id), "pkg.a", "pkg.b.newfn"
    ) == [(True, "high")]
    # 全量：重解析 a.py 后同一名字解析得到，升级为 high。二者差异正是该保守边界本身。
    assert _import_edges(
        RepositoryIndex(fresh_stores, workspace.repository_id), "pkg.a", "pkg.b.newfn"
    ) == [(True, "high")]


# ----------------------------- 遗留边界 #3：同名多目标部分删除时 confidence 不重算


def test_incremental_recomputes_confidence_when_redundant_target_removed(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    """Phase26: complete association closure must agree with fresh indexing; NOT EXECUTED."""
    root = tmp_path / "p"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", "class thing:\n    pass\n\n\ndef thing() -> int:\n    return 1\n")
    _write(root, "pkg/caller.py", "from pkg.mod import thing\n\n\nx = thing\n")
    workspace = _ws(root, "repo_bnd_conf")
    indexer = Indexer()
    indexer.index(stores, workspace)  # 首次：thing 两个同名目标 → caller 的 IMPORT 都是 low

    # 删掉 class thing，只留 def thing；caller 内容不变。
    _write(root, "pkg/mod.py", "def thing() -> int:\n    return 1\n")
    stats = indexer.index(stores, workspace)
    assert "pkg/caller.py" not in stats.parsed_paths  # caller 未重解析

    Indexer().index(fresh_stores, workspace)

    # 全量：thing 现在唯一 → 已解析的 IMPORT 边升到 high。
    fresh_edges = _import_edges(
        RepositoryIndex(fresh_stores, workspace.repository_id), "pkg.caller", "pkg.mod.thing"
    )
    assert (True, "high") in fresh_edges
    # 增量重算剩余目标的置信档位，必须与全量相同。
    incremental = _import_edges(
        RepositoryIndex(stores, workspace.repository_id), "pkg.caller", "pkg.mod.thing"
    )
    assert (True, "high") in incremental
    assert sorted(incremental) == sorted(fresh_edges)
    assert incremental  # 仍有边（悬挂收敛后的未解析 low），只是没升级


# --------------------------- 遗留边界 #5：RepositoryIndex 每次查询不缓存 + 增量走全量读


def test_repository_index_requeries_without_caching(stores: Stores, tmp_path: Path) -> None:
    """钉住遗留边界 #5（index.py 类 docstring + §3）：``RepositoryIndex`` 每次方法调用
    都重新查询、不缓存行。两次 ``symbols()`` 应各触发一次底层 ``list_by_repo``。
    """
    root = tmp_path / "p"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/m.py", "def f() -> int:\n    return 1\n")
    workspace = _ws(root, "repo_bnd_nocache")
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)

    calls = {"n": 0}
    original = stores.symbols.list_by_repo

    def counting(rid: str):
        calls["n"] += 1
        return original(rid)

    stores.symbols.list_by_repo = counting
    try:
        first = index.symbols()
        second = index.symbols()
    finally:
        stores.symbols.list_by_repo = original

    assert first and second
    assert calls["n"] == 2  # 每次调用都新查询；若做了缓存只会是 1


def test_incremental_reads_known_symbols_via_full_repo_query(
    stores: Stores, tmp_path: Path
) -> None:
    """钉住遗留边界 #5 的另一半：增量索引为了拿到未变化文件的 known 符号，走
    ``list_by_repo`` **全量读**（不按 file_id 分批 IN，SQLite 999 变量上限是原因）。
    """
    root = tmp_path / "p"
    _write(root, "pkg/__init__.py", "")
    _write(
        root, "pkg/a.py", "from pkg.b import helper\n\n\ndef use() -> int:\n    return helper()\n"
    )
    _write(root, "pkg/b.py", "def helper() -> int:\n    return 1\n")
    workspace = _ws(root, "repo_bnd_fullread")
    Indexer().index(stores, workspace)

    # 改 b.py 触发一次增量：a.py 未变，作为 known 符号需要从库里全量读回。
    _write(root, "pkg/b.py", "def helper() -> int:\n    return 2\n")
    calls = {"n": 0}
    original = stores.symbols.list_by_repo

    def counting(rid: str):
        calls["n"] += 1
        return original(rid)

    stores.symbols.list_by_repo = counting
    try:
        stats = Indexer().index(stores, workspace)
    finally:
        stores.symbols.list_by_repo = original

    assert stats.incremental
    assert set(stats.parsed_paths) == {"pkg/__init__.py", "pkg/a.py", "pkg/b.py"}
    assert calls["n"] >= 1  # known 符号加载走了一次全量 list_by_repo


@pytest.mark.parametrize("blocked", ["src/miniauth/session.py", "src/miniauth", "."])
def test_unreadable_paths_preserve_index_until_confirmed_deleted(stores, tmp_path, blocked):
    from dataclasses import replace

    from codepilot.repo.scanner import RepositoryScanner

    root = _copy_fixture(tmp_path)
    ws = _ws(root)
    Indexer().index(stores, ws)
    before = _dump(stores, ws.repository_id)

    class UnreadableScanner(RepositoryScanner):
        def scan(self, root):
            result = super().scan(root)
            return replace(
                result,
                files=tuple(
                    f
                    for f in result.files
                    if not (blocked == "." or f.path == blocked or f.path.startswith(blocked + "/"))
                ),
                unreadable=(blocked,),
            )

    with pytest.raises(OSError, match="scan incomplete"):
        Indexer(scanner=UnreadableScanner()).index(stores, ws)
    assert _dump(stores, ws.repository_id) == before
    assert Indexer().index(stores, ws).changed_paths == ()
    (root / "src/miniauth/session.py").unlink()
    stats = Indexer().index(stores, ws)
    assert stats.removed_paths == ("src/miniauth/session.py",)


# Phase26 final closure comparisons: AUTHORED, NOT EXECUTED.
@pytest.mark.parametrize("mutation", ["rename", "new", "delete", "alias", "cycle"])
def test_phase26_complete_association_closure(stores, fresh_stores, tmp_path, mutation):
    root = tmp_path / "closure"
    root.mkdir()
    (root / "a.py").write_text("from b import target as alias\ndef call(): return alias()\n")
    (root / "b.py").write_text("def target(): return 1\n")
    (root / "unrelated.py").write_text("def stable(): return 0\n")
    (root / "test_b.py").write_text(
        "from b import target\ndef test_target(): assert target() == 1\n"
    )
    parser = CountingParser()
    indexer = Indexer(parser=parser)
    indexer.index(stores, _ws(root))
    parser.paths.clear()
    if mutation == "rename":
        (root / "b.py").write_text("def renamed(): return 2\n")
    elif mutation == "new":
        (root / "new.py").write_text("from b import target\n")
        (root / "b.py").write_text("from new import target\n")
    elif mutation == "delete":
        (root / "b.py").unlink()
    elif mutation == "alias":
        (root / "b.py").write_text("def other(): return 2\ntarget = other\n")
    else:
        (root / "b.py").write_text("from a import call\ndef target(): return call()\n")
    stats = indexer.index(stores, _ws(root))
    assert "unrelated.py" not in parser.paths
    assert "unrelated.py" in stats.parse_reused_paths
    Indexer().index(fresh_stores, _ws(root))
    assert _dump(stores, _ws(root).repository_id) == _dump(fresh_stores, _ws(root).repository_id)


def test_phase26_cold_index_rebuilds_old_semantics(stores, fresh_stores, tmp_path):
    root = _copy_fixture(tmp_path)
    Indexer().index(stores, _ws(root))
    # Old persisted edges do not contain complete raw import bindings.
    stores.edges.delete_by_kind(_ws(root).repository_id, "IMPORT")
    stores.repositories.set_index_semantics(_ws(root).repository_id, "legacy-semantics")
    stats = Indexer().index(stores, _ws(root))
    Indexer().index(fresh_stores, _ws(root))
    assert stats.association_mode == "complete_closure_v2" and stats.parsed_paths
    assert _dump(stores, _ws(root).repository_id) == _dump(fresh_stores, _ws(root).repository_id)


# ------------------- Phase28 T07：缓存容量参数（opt-in 实验通道）回归钉
#
# 决策见 docs/notes/phase28-index-decision.md：cache_max_entries / cache_max_bytes
# 只作为剖析与实验的 opt-in 通道，默认 128 / 8MiB 不变。这里钉两件事：
# 两个上界真实生效（复用语义不变——命中仍来自 cached_at_start 快照），以及
# 默认构造与显式 (128, 8MiB) 行为一致（默认语义未被参数化改动）。
# 直接读 indexer._parsed 是有意的：上界本身就在这个结构上生效，stats 只能间接反映。


def test_cache_capacity_parameters_take_effect(stores: Stores, tmp_path: Path) -> None:
    root = tmp_path / "tiny-cache"
    root.mkdir()
    for i in range(3):
        (root / f"mod{i}.py").write_text("def value():\n    return 1\n", encoding="utf8")

    indexer = Indexer(cache_max_entries=2, cache_max_bytes=8 * 1024 * 1024)
    first = indexer.index(stores, _ws(root))
    assert len(first.parsed_paths) == 3
    assert len(indexer._parsed) == 2  # 3 棵树只留 2 棵：条数上界生效

    (root / "mod0.py").write_text("def value():\n    return 2\n", encoding="utf8")
    second = indexer.index(stores, _ws(root))
    assert second.parsed_paths == ("mod0.py",)
    assert len(second.parse_reused_paths) == 2  # 快照命中受 2 条上界约束

    tiny = Indexer(cache_max_entries=128, cache_max_bytes=1)
    # repositories 对 (url, base_commit) 唯一：字节上界检查用独立目录，避免同 URL 复用。
    tiny_root = tmp_path / "tiny-bytes"
    tiny_root.mkdir()
    for i in range(3):
        (tiny_root / f"mod{i}.py").write_text("def value():\n    return 1\n", encoding="utf8")
    tiny_first = tiny.index(stores, _ws(tiny_root, "repo_test_tiny_bytes"))
    assert len(tiny_first.parsed_paths) == 3
    assert len(tiny._parsed) == 0  # 字节上界生效：1 字节连最小的文件都放不下


def test_default_cache_bounds_match_explicit_128(
    stores: Stores, fresh_stores: Stores, tmp_path: Path
) -> None:
    root_default = tmp_path / "bounds-default"
    root_explicit = tmp_path / "bounds-explicit"
    for root in (root_default, root_explicit):
        root.mkdir()
        for i in range(130):
            (root / f"mod{i:03}.py").write_text("def value():\n    return 1\n", encoding="utf8")

    default_indexer = Indexer()
    explicit_indexer = Indexer(cache_max_entries=128, cache_max_bytes=8 * 1024 * 1024)
    default_first = default_indexer.index(stores, _ws(root_default))
    explicit_first = explicit_indexer.index(fresh_stores, _ws(root_explicit))
    assert len(default_first.parsed_paths) == len(explicit_first.parsed_paths) == 130
    assert len(default_indexer._parsed) == len(explicit_indexer._parsed) == 128

    for root in (root_default, root_explicit):
        (root / "mod000.py").write_text("def value():\n    return 2\n", encoding="utf8")
    default_second = default_indexer.index(stores, _ws(root_default))
    explicit_second = explicit_indexer.index(fresh_stores, _ws(root_explicit))
    assert len(default_second.parsed_paths) == len(explicit_second.parsed_paths) == 2
    assert len(default_second.parse_reused_paths) == len(explicit_second.parse_reused_paths) == 128
