"""Phase29 T10：增量索引的受限边替换（语义等价是硬门槛）。

断言口径沿用 R-16（``tests/support/indexdump.dump_index``）：增量重建后的库必须
与"同一仓库状态下的全新全量索引"逐字段一致。本轮新增两条行为断言：

1. **体改动 fast path**：当改动不增删任何 symbol（symbol id 集合不变）时，
   ``replace_for_sources`` 只替换改动文件 symbol 的边，不再全图 DELETE+INSERT；
2. **符号集变化闭包重建（Phase33 T05 契约变更）**：新增/删除/重命名 symbol
   或删除文件时，只重建变更文件及其受影响 importer/reexport 闭包的边，不再
   无条件全图删插——Phase32 剖面证实整图删插是负收益（change 82.5s 比
   cold 68.4s 还慢 20.6%）；结果仍与全量索引逐字段一致。

两条路径都必须通过 dump_index 等价断言；fast path 与闭包重建的选择只由
symbol 集合是否变化决定，不由性能决定。等价性断言一律保留不放宽；被替换的
只有旧"回退全图替换"间谍断言（any(len(ids) >= total_symbols)），新契约见
test_symbol_addition_rebuilds_affected_closure_and_stays_equivalent。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.repo.index import Indexer
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import EdgeStore, Stores
from support.indexdump import dump_index

HUB = "pkg/hub.py"
LEAF = "pkg/leaf.py"


def _ws(path: Path, repository_id: str = "repo_p29") -> Workspace:
    return Workspace(
        path=path,
        source=normalize_source(str(path)),
        base_commit="0" * 40,
        repository_id=repository_id,
    )


def make_stores() -> Stores:
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    return Stores(create_session_factory(engine)())


def build_workspace(tmp_path: Path, name: str = "phase29-incr") -> Path:
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / HUB).write_text(
        "def hub_add(value: int) -> int:\n    return value + 1\n", encoding="utf-8"
    )
    (root / LEAF).write_text(
        "from pkg.hub import hub_add\n\n\ndef leaf_run(value: int) -> int:\n"
        "    return hub_add(value) * 2\n",
        encoding="utf-8",
    )
    (root / "pkg" / "extra.py").write_text(
        "from pkg.hub import hub_add\n\n\ndef extra_use() -> int:\n    return hub_add(2)\n",
        encoding="utf-8",
    )
    return root


def index_fresh(root: Path, repository_id: str) -> tuple[Stores, Indexer]:
    stores = make_stores()
    indexer = Indexer(parser=TreeSitterParser())
    indexer.index(stores, _ws(root, repository_id))
    stores.session.commit()
    return stores, indexer


def capture_replace(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the source-id sets handed to EdgeStore.replace_for_sources."""
    calls: dict[str, list] = {"source_ids": []}
    original = EdgeStore.replace_for_sources

    def spy(self: EdgeStore, repository_id: str, source_ids: list[str], rows: object) -> None:
        calls["source_ids"].append(list(source_ids))
        return original(self, repository_id, source_ids, rows)

    monkeypatch.setattr(EdgeStore, "replace_for_sources", spy)
    return calls


def test_body_only_change_replaces_only_changed_file_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_workspace(tmp_path)
    repository_id = "repo_p29_fast"
    stores, indexer = index_fresh(root, repository_id)

    calls = capture_replace(monkeypatch)
    # Body-only change: same symbols (ids are content-independent), no adds/removes.
    (root / HUB).write_text(
        "def hub_add(value: int) -> int:\n    return value + 2  # phase29 touch\n",
        encoding="utf-8",
    )
    stats = indexer.index(stores, _ws(root, repository_id))
    stores.session.commit()
    incremental_dump = dump_index(stores, repository_id)

    replaced = [ids for ids in calls["source_ids"] if ids]
    assert replaced, "fast path must still replace the changed file's edges"
    total_symbols = len({s.id for s in stores.symbols.list_by_repo(repository_id)})
    for ids in replaced:
        assert len(ids) < total_symbols, (
            f"body-only change must not rewrite the whole graph: {len(ids)} >= {total_symbols}"
        )
    # The hub symbols themselves must be inside the replaced set.
    hub_ids = {
        s.id for s in stores.symbols.list_by_repo(repository_id) if "hub" in s.qualified_name
    }
    assert hub_ids & set(replaced[0])

    # Equivalence against a fresh full index of the modified workspace.
    ref_stores, _ = index_fresh(root, repository_id)
    assert incremental_dump[1:] == dump_index(ref_stores, repository_id)[1:]
    assert stats.association_mode == "complete_closure_v2"


def test_symbol_addition_rebuilds_affected_closure_and_stays_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase33 T05 契约变更：符号集变化不再全图删插，只重建受影响闭包。

    旧 Phase29 断言 any(len(ids) >= total_symbols)（全图替换间谍）在剖面中
    被证实是负收益来源（change 82.5s > cold 68.4s，15 万条边整图删插），
    经 lead 批准替换为闭包契约：replaced 源集恰为闭包文件的符号、闭包 ⊇
    变更文件、⊊ 全图、与 index 内部计算的 edge_closure_paths 一致。
    dump_index 等价门槛原样保留。
    """
    root = build_workspace(tmp_path)
    repository_id = "repo_p29_add"
    stores, indexer = index_fresh(root, repository_id)

    calls = capture_replace(monkeypatch)
    (root / HUB).write_text(
        "def hub_add(value: int) -> int:\n    return value + 1\n\n\n"
        "def hub_new_function(value: int) -> int:\n    return value * 3\n",
        encoding="utf-8",
    )
    stats = indexer.index(stores, _ws(root, repository_id))
    stores.session.commit()

    # 闭包 = 变更文件 + 绑定脏模块 pkg.hub 的 importer（leaf/extra 都是
    # from pkg.hub import hub_add）；pkg/__init__.py 无 import，不入闭包。
    assert stats.fast_mode is False
    assert set(stats.edge_closure_paths) == {HUB, LEAF, "pkg/extra.py"}

    all_symbols = stores.symbols.list_by_repo(repository_id)
    path_by_file = {f.id: f.path for f in stores.files.list_by_repo(repository_id)}
    closure_ids = {
        s.id for s in all_symbols if path_by_file[s.file_id] in set(stats.edge_closure_paths)
    }
    hub_ids = {s.id for s in all_symbols if "hub" in s.qualified_name}

    replaced = [ids for ids in calls["source_ids"] if ids]
    # 主 replace 恰好一次；dangling/TEST 替换的 sources 为空，已被过滤。
    assert replaced
    assert all(set(ids) == closure_ids for ids in replaced)
    assert hub_ids <= closure_ids
    assert closure_ids < {s.id for s in all_symbols}  # 真子集：闭包 ⊊ 全图

    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]


def test_file_removal_falls_back_and_stays_equivalent(tmp_path: Path) -> None:
    root = build_workspace(tmp_path)
    repository_id = "repo_p29_rm"
    stores, indexer = index_fresh(root, repository_id)
    (root / LEAF).unlink()
    indexer.index(stores, _ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]
    assert not any("leaf" in f.path for f in stores.files.list_by_repo(repository_id))


def test_body_change_on_test_file_updates_test_edges_equivalently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build_workspace(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_hub.py").write_text(
        "from pkg.hub import hub_add\n\n\ndef test_hub_add() -> None:\n    assert hub_add(1) == 2\n",
        encoding="utf-8",
    )
    repository_id = "repo_p29_test"
    stores, indexer = index_fresh(root, repository_id)

    calls = capture_replace(monkeypatch)
    (root / "tests" / "test_hub.py").write_text(
        "from pkg.hub import hub_add\n\n\ndef test_hub_add() -> None:\n"
        "    assert hub_add(1) == 2  # touched\n",
        encoding="utf-8",
    )
    indexer.index(stores, _ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]
    replaced = [ids for ids in calls["source_ids"] if ids]
    assert replaced
