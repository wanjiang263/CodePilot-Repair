"""``inventory.catalog`` 的现有测试。"""

from __future__ import annotations

from inventory.catalog import describe, skus


def test_describe_known_sku() -> None:
    assert describe("A-1") == "A-1 - Widget"


def test_describe_with_custom_catalog() -> None:
    assert describe("X", catalog={"X": "Doohickey"}) == "X - Doohickey"


def test_skus_are_sorted() -> None:
    assert skus() == ["A-1", "B-2", "C-3"]


def test_skus_of_custom_catalog() -> None:
    assert skus(catalog={"b": "2", "a": "1"}) == ["a", "b"]
