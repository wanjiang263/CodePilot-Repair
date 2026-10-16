"""``inventory.stock`` 的现有测试。"""

from __future__ import annotations

from inventory.stock import Line, Stock


def test_restock_accumulates() -> None:
    stock = Stock()
    stock.restock("A-1", 3)
    stock.restock("A-1", 2)
    assert stock.available("A-1") == 5


def test_reserve_reduces_available() -> None:
    stock = Stock()
    stock.restock("A-1", 5)
    stock.reserve(Line("A-1", 2))
    assert stock.available("A-1") == 3


def test_unknown_sku_is_zero() -> None:
    assert Stock().available("Z-9") == 0


def test_value_at_uses_price_table() -> None:
    stock = Stock()
    stock.restock("A-1", 3)
    assert stock.value_at({"A-1": 2.5}) == 7.5
