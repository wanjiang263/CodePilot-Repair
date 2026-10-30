"""Phase28 索引+存储闭环集成：真 git 仓库 → 真 workspace → 真 SQLite 文件落库。

与 ``tests/unit/repo/test_index.py`` / ``tests/integration/test_incremental.py`` 的分工：
单测在纯目录上钉字段级行为；本文件把 **Indexer 默认参数的闭环语义** 与
**Phase28 新增 cache_max_entries 参数的"不改变语义"承诺** 放进同一条真实链路
（WorkspaceManager fetch 模式 + 磁盘 SQLite）里验证：

1. 冷索引进临时 SQLite：stats 与库行数一致，闭包模式为 complete_closure_v2；
2. 改 1 个文件再索引：只重解析它，其余从解析缓存快照复用，库逐字段等于
   "同一仓库状态下的全新全量索引"；
3. ``cache_max_entries=2`` 复跑：条数上界真实生效，且库内容与默认参数运行逐字段
   相等——参数只影响内存缓存容量，绝不改变索引语义（docs/notes/phase28-index-decision.md）。

零网络、零 Docker、零模型请求；一切写都在 ``tmp_path``。
"""

from __future__ import annotations

from pathlib import Path

from codepilot.repo.index import Indexer
from codepilot.repo.workspace import WorkspaceManager
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import Stores
from support.gitfixture import init_repo
from support.indexdump import dump_index

REPO_FILES = {
    "src/app/__init__.py": "",
    "src/app/core.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
    "src/app/util.py": "def helper(n: int) -> int:\n    return n * 2\n",
    "tests/test_core.py": "from app.core import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n",
    "README.md": "# phase28 loop\n",
}
PY_PATHS = {"src/app/__init__.py", "src/app/core.py", "src/app/util.py", "tests/test_core.py"}


def _stores(db_path: Path) -> Stores:
    engine = create_engine_from_url(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    factory = create_session_factory(engine)
    return Stores(factory())


def test_index_storage_loop_with_default_and_capped_cache(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", REPO_FILES)
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    workspace = manager.create(
        source=str(repo.path), base_commit=repo.head, target=tmp_path / "ws", mode="fetch"
    )
    repository_id = workspace.repository_id

    # ---- 冷索引：默认参数 Indexer，落临时 SQLite 文件 ----
    db_a = tmp_path / "default.db"
    stores_a = _stores(db_a)
    default_indexer = Indexer()
    first = default_indexer.index(stores_a, workspace)

    assert first.incremental is False
    assert first.association_mode == "complete_closure_v2"
    assert set(first.parsed_paths) == PY_PATHS
    assert first.files == len(REPO_FILES)  # README.md 也入 files 表，只是不参与解析
    assert first.tests == 1  # tests/test_core.py::test_add
    # stats 与库行数互相印证，且确实写进了磁盘 SQLite 文件。
    assert len(stores_a.files.list_by_repo(repository_id)) == first.files
    assert (
        len(stores_a.symbols.list_by_repo(repository_id)) == sum(first.symbols_by_kind.values()) > 0
    )
    assert (
        len(stores_a.edges.list_by_repo(repository_id))
        == sum(first.edges_by_kind_confidence.values())
        > 0
    )
    assert db_a.exists() and db_a.stat().st_size > 0

    # ---- 增量：改 1 个文件，只重解析它，其余走解析缓存复用 ----
    (workspace.path / "src" / "app" / "core.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b + 0\n",
        encoding="utf-8",
        newline="\n",
    )
    second = default_indexer.index(stores_a, workspace)
    assert second.incremental is True
    assert second.parsed_paths == ("src/app/core.py",)
    assert second.changed_paths == ("src/app/core.py",)
    assert set(second.parse_reused_paths) == PY_PATHS - {"src/app/core.py"}
    assert second.association_mode == "complete_closure_v2"

    # ---- Phase28 缓存参数复跑：条数上界生效，语义与默认参数逐字段一致 ----
    stores_b = _stores(tmp_path / "capped.db")
    capped = Indexer(cache_max_entries=2)
    third = capped.index(stores_b, workspace)
    assert third.incremental is False
    assert len(capped._parsed) == 2  # 4 棵解析树只留 2 棵：上界真实生效
    # 增量后的库 == 上界参数下"同一仓库状态的全新全量"库：参数不改变语义。
    assert dump_index(stores_a, repository_id) == dump_index(stores_b, repository_id)

    before = dump_index(stores_b, repository_id)
    fourth = capped.index(stores_b, workspace)
    assert fourth.parsed_paths == ()  # 无变化快路径不受参数影响
    assert dump_index(stores_b, repository_id) == before

    manager.remove(workspace)
