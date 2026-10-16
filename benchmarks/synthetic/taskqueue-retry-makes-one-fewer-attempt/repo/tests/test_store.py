"""``taskqueue.store`` 的现有测试。"""

from __future__ import annotations

from taskqueue.store import Store


def test_set_then_get() -> None:
    store = Store()
    store.set("a", "value")
    assert store.get("a") == "VALUE"


def test_delete_removes_the_key() -> None:
    store = Store()
    store.set("a", "x")
    store.delete("a")
    assert store.keys() == []


def test_keys_are_sorted() -> None:
    store = Store()
    store.set("b", "1")
    store.set("a", "2")
    assert store.keys() == ["a", "b"]
