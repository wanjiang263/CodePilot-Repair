"""Phase30 T07: incremental index re-export staleness (failing tests first).

A body-only edit that retargets an import (``from pkg.a import value`` ->
``from pkg.b import value``) keeps every symbol id, so the Phase29 fast path
engaged — but the resolution of every downstream file that re-exports THROUGH
the changed module changed too, and the fast path left those edges stale. The
contract (R-16, tests/support/indexdump.dump_index): an incremental re-index
must be field-identical to a fresh full index of the same workspace state,
indexed under the same ``repository_id`` (symbol ids embed it).
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

A_SRC = "def value() -> int:\n    return 1\n"
B_SRC = "def value() -> int:\n    return 2\n"
CONSUMER = "from pkg.hub import value\n\n\ndef use() -> int:\n    return value()\n"


def make_stores() -> Stores:
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    return Stores(create_session_factory(engine)())


def ws(path: Path, repository_id: str) -> Workspace:
    return Workspace(
        path=path,
        source=normalize_source(str(path)),
        base_commit="0" * 40,
        repository_id=repository_id,
    )


def index_fresh(root: Path, repository_id: str) -> tuple[Stores, Indexer]:
    stores = make_stores()
    indexer = Indexer(parser=TreeSitterParser())
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()
    return stores, indexer


def build_workspace(tmp_path: Path, name: str, *, alias: bool = False) -> Path:
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(A_SRC, encoding="utf-8")
    (root / "pkg" / "b.py").write_text(B_SRC, encoding="utf-8")
    hub_line = "from pkg.a import value as v\n" if alias else "from pkg.a import value\n"
    (root / "pkg" / "hub.py").write_text(hub_line, encoding="utf-8")
    (root / "pkg" / "consumer.py").write_text(CONSUMER, encoding="utf-8")
    return root


def capture_replace(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"source_ids": []}
    original = EdgeStore.replace_for_sources

    def spy(self: EdgeStore, repository_id: str, source_ids: list[str], rows: object) -> None:
        calls["source_ids"].append(list(source_ids))
        return original(self, repository_id, source_ids, rows)

    monkeypatch.setattr(EdgeStore, "replace_for_sources", spy)
    return calls


def test_reexport_retarget_is_incrementally_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pkg/hub.py swaps its re-export source: downstream edges must follow."""
    root = build_workspace(tmp_path, "p30_reexport")
    repository_id = "repo_p30_reexport"
    stores, indexer = index_fresh(root, repository_id)
    capture_replace(monkeypatch)

    (root / "pkg" / "hub.py").write_text("from pkg.b import value\n", encoding="utf-8")
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    incremental = dump_index(stores, repository_id)[1:]
    full = dump_index(ref_stores, repository_id)[1:]
    assert incremental == full, "incremental re-index diverged from a fresh full index"
    # The consumer must now resolve through pkg.b.value, not pkg.a.value.
    assert any("pkg.b.value" in repr(row) for row in full), (
        "consumer edges must resolve through the new re-export target"
    )


def test_reexport_alias_retarget_is_incrementally_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from pkg.a import value as v -> from pkg.b import value as v."""
    root = build_workspace(tmp_path, "p30_alias", alias=True)
    repository_id = "repo_p30_alias"
    stores, indexer = index_fresh(root, repository_id)
    capture_replace(monkeypatch)

    (root / "pkg" / "hub.py").write_text("from pkg.b import value as v\n", encoding="utf-8")
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]


def test_two_level_reexport_chain_updates_downstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """consumer -> mid -> hub -> a/b: changing hub must refresh mid and consumer."""
    root = tmp_path / "p30_chain"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text(A_SRC, encoding="utf-8")
    (root / "pkg" / "b.py").write_text(B_SRC, encoding="utf-8")
    (root / "pkg" / "hub.py").write_text("from pkg.a import value\n", encoding="utf-8")
    (root / "pkg" / "mid.py").write_text("from pkg.hub import value\n", encoding="utf-8")
    (root / "pkg" / "consumer.py").write_text(
        "from pkg.mid import value\n\n\ndef use() -> int:\n    return value()\n",
        encoding="utf-8",
    )
    repository_id = "repo_p30_chain"
    stores, indexer = index_fresh(root, repository_id)
    capture_replace(monkeypatch)

    (root / "pkg" / "hub.py").write_text("from pkg.b import value\n", encoding="utf-8")
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:], (
        "two-level re-export chain left stale edges"
    )


def test_consumer_symbol_set_unchanged_but_targets_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same consumer symbols, different IMPORT/CALL targets after the retarget."""
    root = build_workspace(tmp_path, "p30_symbols")
    repository_id = "repo_p30_symbols"
    stores, indexer = index_fresh(root, repository_id)
    capture_replace(monkeypatch)

    before_stores, _ = index_fresh(root, repository_id)
    before = dump_index(before_stores, repository_id)[2]  # edges tuple

    (root / "pkg" / "hub.py").write_text("from pkg.b import value\n", encoding="utf-8")
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()

    ref_stores, _ = index_fresh(root, repository_id)
    after = dump_index(ref_stores, repository_id)[2]
    assert before != after, "consumer edge targets must change with the re-export"
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]
    # Consumer symbol ids are unchanged: only the edge targets moved.
    assert dump_index(stores, repository_id)[1] == dump_index(ref_stores, repository_id)[1]


def test_body_only_change_still_uses_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No import-binding change: the bounded fast path must stay engaged."""
    root = build_workspace(tmp_path, "p30_body")
    repository_id = "repo_p30_body"
    stores, indexer = index_fresh(root, repository_id)
    calls = capture_replace(monkeypatch)

    (root / "pkg" / "a.py").write_text(
        "def value() -> int:\n    return 10  # body-only\n", encoding="utf-8"
    )
    indexer.index(stores, ws(root, repository_id))
    stores.session.commit()

    total_symbols = len(stores.symbols.list_by_repo(repository_id))
    replaced = [ids for ids in calls["source_ids"] if ids]
    assert replaced, "changed file edges must still be replaced"
    for ids in replaced:
        assert len(ids) < total_symbols, (
            f"body-only change must not rewrite the whole graph: {len(ids)} >= {total_symbols}"
        )
    ref_stores, _ = index_fresh(root, repository_id)
    assert dump_index(stores, repository_id)[1:] == dump_index(ref_stores, repository_id)[1:]
