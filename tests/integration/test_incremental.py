"""R-22：增量重建与 git history 越界的**集成**回归。

与 ``tests/unit/repo/test_index.py`` / ``test_git_history.py`` 的分工：单测在
拷贝出来的普通目录或工作副本上验证行为，本文件走**真实的共享缓存 + workspace
路径**——即生产里 ``WorkspaceManager`` 真的会产生的目录布局。两条断言各自独立：

1. 增量重建走真实 workspace（fetch 模式建出来的目录），改一个文件后只重解析它，
   且库快照与"同状态全新全量索引"逐字段相等；再验一次"什么都不改"的重跑。
2. history 越界走**真实共享缓存**：缓存是 fixture 仓库的 bare clone，里面确实
   带着 base 之后的 ref 与对象——这正是必须独立于单测的原因。断言 base 之后的
   提交绝不泄漏进 file_churn / symbol_churn。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from codepilot.repo.git_history import GitHistory
from codepilot.repo.index import Indexer
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.symbols import extract_symbols
from codepilot.repo.workspace import WorkspaceManager
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import Stores
from support.gitfixture import init_repo
from support.indexdump import dump_index

# base 之前：c1 建文件、c2 改 f、c3(=base) 只改 README。
A_V1 = "def f():\n    return 1\n\n\ndef g():\n    return 5\n"
A_V2 = "def f():\n    return 10\n\n\ndef g():\n    return 5\n"
# base 之后：c4 新建 b.py、c5 再改 a.py 的 g。二者都绝不能出现在历史特征里。
A_V3 = "def f():\n    return 10\n\n\ndef g():\n    return 7\n"


def _stores(url: str = "sqlite://") -> tuple[Stores, sessionmaker[Session]]:
    engine = create_engine_from_url(url)
    init_db(engine)
    factory = create_session_factory(engine)
    return Stores(factory()), factory


# ---------------------------------------------------------------- 断言 1：增量重建


def test_incremental_reindex_over_real_workspace(tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path / "repo",
        {
            "src/app/__init__.py": "",
            "src/app/core.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
            "README.md": "# r\n",
        },
    )
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    workspace = manager.create(
        source=str(repo.path), base_commit=repo.head, target=tmp_path / "ws", mode="fetch"
    )

    stores, _ = _stores()
    indexer = Indexer()
    first = indexer.index(stores, workspace)
    assert not first.incremental
    assert set(first.parsed_paths) == {"src/app/__init__.py", "src/app/core.py"}

    # 在 workspace 里改一个文件（不提交）：增量索引只应重解析它。
    core = workspace.path / "src" / "app" / "core.py"
    core.write_text(
        "def add(a: int, b: int) -> int:\n    return a + b + 0\n",
        encoding="utf-8",
        newline="\n",
    )
    second = indexer.index(stores, workspace)
    assert second.incremental
    assert second.parsed_paths == ("src/app/core.py",)
    assert second.changed_paths == ("src/app/core.py",)

    # 等价性：与"同一 workspace 状态下的一次全新全量索引"逐字段一致。
    fresh, _ = _stores()
    fresh_stats = Indexer().index(fresh, workspace)
    assert dump_index(stores, workspace.repository_id) == dump_index(
        fresh, fresh_stats.repository_id
    )

    # 什么都不改再跑一次：不重解析任何文件，快照不变。
    before = dump_index(stores, workspace.repository_id)
    third = indexer.index(stores, workspace)
    assert third.parsed_paths == ()
    assert dump_index(stores, workspace.repository_id) == before

    manager.remove(workspace)


# ---------------------------------------------------------------- 断言 2：history 越界


def test_history_never_leaks_post_base_commits_through_shared_cache(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": A_V1, "README.md": "# r\n"})
    repo.write("a.py", A_V2)
    repo.commit("c2-change-f")
    repo.write("README.md", "# r v2\n")
    base = repo.commit("c3-readme")  # ← base：只改 README，不改 a.py
    repo.write("b.py", "def h():\n    return 9\n")
    repo.commit("c4-new-file")  # base 之后：新建文件
    repo.write("a.py", A_V3)
    repo.commit("c5-change-g")  # base 之后：改 g

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    workspace = manager.create(
        source=str(repo.path), base_commit=base, target=tmp_path / "ws", mode="fetch"
    )

    # 走**真实共享缓存**：缓存是 fixture 仓库的 bare clone，里面带着 c4/c5 的 ref 与对象。
    history = GitHistory.for_workspace(manager, workspace)
    assert history.repo_path == manager.cache_path(workspace.source)

    churn = history.file_churn()
    # git log <base> 顺序：[c3(0), c2(1), c1(2)]。
    assert churn["a.py"].modifications == 2  # c2 + c1；c5 在 base 之后，不计
    assert churn["a.py"].last_modified_distance == 1  # 最近一次是 c2
    assert churn["README.md"].modifications == 2  # c3 + c1
    assert churn["README.md"].last_modified_distance == 0  # base 自己
    assert "b.py" not in churn  # c4 新建的文件在 base 之后，绝不出现

    # symbol 级：用 base 版本（workspace 里的 a.py = A_V2）的符号做归属。
    parser = TreeSitterParser()
    base_source = (workspace.path / "a.py").read_text(encoding="utf-8")
    assert base_source == A_V2  # 确认 workspace 停在 base，没有被后续提交污染
    symbols = extract_symbols(
        parser.parse(base_source.encode("utf-8"), path="a.py"),
        path="a.py",
        repository_id="r22",
    )
    result = history.symbol_churn(symbols)

    by_qname = {s.qualified_name: s for s in symbols}
    f_churn = result.churn[by_qname["a.f"].id]
    g_churn = result.churn[by_qname["a.g"].id]
    assert f_churn.modifications == 2  # c1(建) + c2(改 f)
    assert f_churn.last_modified_distance == 1
    assert g_churn.modifications == 1  # 只有 c1；c5 改 g 在 base 之后，不计
    assert g_churn.last_modified_distance == 2
    # 只走了 base 的 3 个祖先提交（c1/c2/c3），c4/c5 没被扫到。
    assert result.commits_examined == 3

    manager.remove(workspace)
