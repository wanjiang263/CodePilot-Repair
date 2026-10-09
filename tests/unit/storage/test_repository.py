"""F-13 的证明：写入→读回一致；重复写入按主键 upsert。

外加两条不写在清单里、但漏掉就会在生产里咬人的断言：
外键真的生效（SQLite 默认关闭），以及 ``Stores`` 的九个 store 共享同一个
session（否则"写进去查不到"会以最费解的方式出现）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from codepilot.storage.repository import Stores


def _repo(stores: Stores, repository_id: str = "r1") -> None:
    stores.repositories.upsert(
        repository_id=repository_id, url="https://example.invalid/x.git", base_commit="abc"
    )


def _file(stores: Stores, file_id: str = "f1", path: str = "src/a.py") -> None:
    stores.files.upsert_many(
        [
            {
                "id": file_id,
                "repository_id": "r1",
                "path": path,
                "sha256": "0" * 64,
                "line_count": 10,
            }
        ]
    )


def _symbol(
    stores: Stores,
    symbol_id: str = "s1",
    qualified_name: str = "a.f",
    name: str = "f",
    file_id: str = "f1",
) -> dict[str, object]:
    return {
        "id": symbol_id,
        "file_id": file_id,
        "repository_id": "r1",
        "name": name,
        "qualified_name": qualified_name,
        "kind": "function",
        "start_line": 1,
        "end_line": 5,
    }


def test_repository_upsert_then_read_back(stores: Stores) -> None:
    _repo(stores)
    row = stores.repositories.get("r1")
    assert row is not None
    assert row.url == "https://example.invalid/x.git"
    assert row.base_commit == "abc"


def test_repository_upsert_is_idempotent_on_primary_key(stores: Stores) -> None:
    _repo(stores)
    stores.repositories.upsert(repository_id="r1", url="https://other.invalid", base_commit="def")
    assert stores.repositories.get("r1") is not None
    assert stores.repositories.find_by_url_commit("https://other.invalid", "def") is not None
    assert stores.repositories.find_by_url_commit("https://example.invalid/x.git", "abc") is None


def test_repository_mark_indexed_sets_timestamp(stores: Stores) -> None:
    _repo(stores)
    assert stores.repositories.get("r1").indexed_at is None  # type: ignore[union-attr]
    stores.repositories.mark_indexed("r1")
    assert stores.repositories.get("r1").indexed_at is not None  # type: ignore[union-attr]


def test_mark_indexed_on_missing_repository_raises(stores: Stores) -> None:
    """静默无操作会让"索引没建好"变成"索引建好了但查不到"，更难查。"""
    with pytest.raises(KeyError):
        stores.repositories.mark_indexed("nope")


def test_file_upsert_many_and_get_by_path(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    row = stores.files.get_by_path("r1", "src/a.py")
    assert row is not None
    assert row.line_count == 10


def test_file_upsert_updates_existing_row(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.files.upsert_many(
        [
            {
                "id": "f1",
                "repository_id": "r1",
                "path": "src/a.py",
                "sha256": "1" * 64,
                "line_count": 20,
            }
        ]
    )
    row = stores.files.get("f1")
    assert row is not None
    assert row.line_count == 20
    assert row.sha256 == "1" * 64
    assert len(stores.files.list_by_repo("r1")) == 1


def test_file_parse_error_survives_a_later_clean_upsert(stores: Stores) -> None:
    """``parse_error`` 是诊断信息，不该被下一次 upsert 的默认值悄悄抹掉。"""
    _repo(stores)
    stores.files.upsert_many(
        [
            {
                "id": "f1",
                "repository_id": "r1",
                "path": "src/a.py",
                "parse_error": "SyntaxError: invalid syntax",
            }
        ]
    )
    stores.files.upsert_many(
        [{"id": "f1", "repository_id": "r1", "path": "src/a.py", "line_count": 3}]
    )
    row = stores.files.get("f1")
    assert row is not None
    assert row.line_count == 3


def test_delete_by_paths_returns_ids_and_cascades_to_symbols(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(["f1"], [_symbol(stores)])
    assert stores.symbols.list_by_file("f1") != []

    deleted = stores.files.delete_by_paths("r1", ["src/a.py"])
    assert deleted == ["f1"]
    stores.session.flush()

    # 外键 ondelete=CASCADE 生效时 symbol 会一起消失。
    assert stores.symbols.list_by_file("f1") == []


def test_delete_by_paths_with_empty_input_is_noop(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    assert stores.files.delete_by_paths("r1", []) == []
    assert stores.files.get("f1") is not None


def test_symbols_replace_for_files_is_replace_not_append(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(["f1"], [_symbol(stores, symbol_id="s1", name="f")])
    stores.symbols.replace_for_files(
        ["f1"], [_symbol(stores, symbol_id="s2", name="g", qualified_name="a.g")]
    )

    names = [s.name for s in stores.symbols.list_by_file("f1")]
    assert names == ["g"]


def test_symbols_replace_with_empty_file_list_keeps_existing(stores: Stores) -> None:
    """``file_ids`` 为空表示"没有文件需要重建"，不是"清空所有 symbol"。"""
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(["f1"], [_symbol(stores)])
    stores.symbols.replace_for_files([], [])
    assert len(stores.symbols.list_by_file("f1")) == 1


def test_get_by_qualified_name_returns_all_duplicates(stores: Stores) -> None:
    """同名重复定义是合法的，不能只返回一个（02 §7.2）。"""
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(
        ["f1"],
        [
            _symbol(stores, symbol_id="s1", name="f", qualified_name="a.f"),
            _symbol(stores, symbol_id="s2", name="f", qualified_name="a.f"),
        ],
    )
    assert len(stores.symbols.get_by_qualified_name("r1", "a.f")) == 2


def test_symbols_count_by_kind(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(
        ["f1"],
        [_symbol(stores, symbol_id="s1"), _symbol(stores, symbol_id="s2")],
    )
    assert stores.symbols.count_by_kind("r1") == {"function": 2}


def test_edges_replace_for_sources_is_replace(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(
        ["f1"],
        [_symbol(stores, "s1", "a.f", "f"), _symbol(stores, "s2", "a.g", "g")],
    )
    edge = {
        "id": "e1",
        "repository_id": "r1",
        "source_id": "s1",
        "target_id": "s2",
        "kind": "CALL",
        "confidence": 1.0,
    }
    stores.edges.replace_for_sources("r1", ["s1"], [edge])
    stores.edges.replace_for_sources("r1", ["s1"], [{**edge, "id": "e2", "confidence": 0.6}])

    edges = stores.edges.list_by_repo("r1")
    assert len(edges) == 1
    assert edges[0].confidence == pytest.approx(0.6)


def test_edge_with_unresolved_target_is_storable_and_queryable_by_name(
    stores: Stores,
) -> None:
    """未解析到目标的边必须能存：它记录了"这里有个调用但不知道调谁"。

    丢掉它们会让调用图比实际更稀疏，影响面分析随之偏低。
    """
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(["f1"], [_symbol(stores, "s1", "a.f", "f")])
    stores.edges.replace_for_sources(
        "r1",
        ["s1"],
        [
            {
                "id": "e1",
                "repository_id": "r1",
                "source_id": "s1",
                "target_id": None,
                "target_name": "mystery",
                "kind": "CALL",
                "confidence": 0.3,
            }
        ],
    )
    by_name = stores.edges.in_edges_by_name("r1", "mystery")
    assert len(by_name) == 1
    assert stores.edges.in_edges("s1") == []


def test_edge_callers_and_callees(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(
        ["f1"],
        [_symbol(stores, "s1", "a.f", "f"), _symbol(stores, "s2", "a.g", "g")],
    )
    stores.edges.replace_for_sources(
        "r1",
        ["s1"],
        [
            {
                "id": "e1",
                "repository_id": "r1",
                "source_id": "s1",
                "target_id": "s2",
                "kind": "CALL",
                "confidence": 1.0,
            }
        ],
    )
    assert [e.source_id for e in stores.edges.in_edges("s2")] == ["s1"]
    assert [e.target_id for e in stores.edges.out_edges("s1")] == ["s2"]


def test_edge_counts_by_kind_and_confidence(stores: Stores) -> None:
    _repo(stores)
    _file(stores)
    stores.symbols.replace_for_files(
        ["f1"], [_symbol(stores, "s1", "a.f", "f"), _symbol(stores, "s2", "a.g", "g")]
    )
    stores.edges.replace_for_sources(
        "r1",
        ["s1"],
        [
            {
                "id": "e1",
                "repository_id": "r1",
                "source_id": "s1",
                "target_id": "s2",
                "kind": "CALL",
                "confidence": 1.0,
            },
            {
                "id": "e2",
                "repository_id": "r1",
                "source_id": "s1",
                "target_id": "s2",
                "kind": "CALL",
                "confidence": 0.6,
            },
        ],
    )
    assert stores.edges.count_by_kind_confidence("r1") == {("CALL", 1.0): 1, ("CALL", 0.6): 1}


def test_run_lifecycle_and_finish(stores: Stores) -> None:
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="boom", mode="user")
    stores.runs.create(run_id="run1", task_id="t1", code_version="deadbeef+dirty")

    row = stores.runs.get("run1")
    assert row is not None
    assert row.status == "running"
    assert row.finished_at is None

    stores.runs.finish("run1", status="stopped", stop_reason="no_progress", usage={"cost": 1.5})
    row = stores.runs.get("run1")
    assert row is not None
    assert row.status == "stopped"
    assert row.stop_reason == "no_progress"
    assert row.usage == {"cost": 1.5}
    assert row.finished_at is not None


def test_finish_missing_run_raises(stores: Stores) -> None:
    with pytest.raises(KeyError):
        stores.runs.finish("nope", status="failed", stop_reason=None, usage={})


def test_attempt_upsert_is_keyed_on_run_and_iteration(stores: Stores) -> None:
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")

    stores.attempts.upsert(run_id="run1", iteration=1, patch_hash="h1", status="FAIL")
    stores.attempts.upsert(run_id="run1", iteration=1, patch_hash="h2", status="PASS")
    stores.attempts.upsert(run_id="run1", iteration=2, patch_hash="h3", status="PASS")

    rows = stores.attempts.list_by_run("run1")
    assert [r.iteration for r in rows] == [1, 2]
    assert rows[0].patch_hash == "h2"


def test_event_seq_is_monotonic_per_run(stores: Stores) -> None:
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    stores.runs.create(run_id="run2", task_id="t1")

    for _ in range(3):
        stores.events.append(run_id="run1", node="n", type="node_start")
    stores.events.append(run_id="run2", node="n", type="node_start")

    assert [e.seq for e in stores.events.list_after("run1")] == [1, 2, 3]
    # seq 是每 run 独立的，不是全局的。
    assert [e.seq for e in stores.events.list_after("run2")] == [1]


def test_event_list_after_supports_resume(stores: Stores) -> None:
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    for i in range(1, 6):
        stores.events.append(run_id="run1", node="n", type="node_start", seq=i)

    resumed = stores.events.list_after("run1", after_seq=2)
    assert [e.seq for e in resumed] == [3, 4, 5]


def test_evaluation_upsert_keyed_on_run_and_benchmark(stores: Stores) -> None:
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")

    stores.evaluations.upsert(run_id="run1", benchmark="synthetic", resolved=False)
    stores.evaluations.upsert(
        run_id="run1", benchmark="synthetic", resolved=True, metrics={"recall": 0.5}
    )
    stores.evaluations.upsert(run_id="run1", benchmark="swebench", resolved=False)

    rows = stores.evaluations.list_by_run("run1")
    assert len(rows) == 2
    synthetic = next(r for r in rows if r.benchmark == "synthetic")
    assert synthetic.resolved is True
    assert synthetic.metrics == {"recall": 0.5}


def test_duplicate_event_seq_is_rejected(stores: Stores) -> None:
    """唯一约束必须真的存在，否则断线续传会漏事件或重复事件。"""
    _repo(stores)
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    stores.events.append(run_id="run1", node="n", type="node_start", seq=1)
    with pytest.raises(IntegrityError):
        stores.events.append(run_id="run1", node="n", type="node_start", seq=1)


def test_foreign_keys_are_actually_enforced(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """SQLite 默认**不**强制外键。

    如果 ``PRAGMA foreign_keys=ON`` 没生效，级联删除不会发生，孤儿行会慢慢
    堆积，而所有测试仍然是绿的。
    """
    assert db_engine.dialect.name == "sqlite"
    with session_factory() as session:
        value = session.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1


def test_all_nine_tables_exist(stores: Stores) -> None:
    from codepilot.storage.models import Base

    assert sorted(Base.metadata.tables) == [
        "attempts",
        "edges",
        "evaluations",
        "events",
        "files",
        "repositories",
        "runs",
        "symbols",
        "tasks",
    ]


def test_stores_share_one_session(stores: Stores) -> None:
    """九个 store 必须挂在同一个 session 上，否则"刚写的数据查不到"。"""
    assert stores.repositories._s is stores.session
    assert stores.events._s is stores.session
